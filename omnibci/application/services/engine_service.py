"""
NeuroEngineService — Application Service.

Orchestrates NeuroEngine lifecycle and exposes it to the API layer via
command / query objects.  The API layer never touches NeuroEngine directly.
"""

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from threading import RLock, Thread
from typing import Callable, Optional, TYPE_CHECKING
from uuid import uuid4

from omnibci.application.runtime_admission import RuntimeAdmission, admitted
from omnibci.application.activation_settings import SECTION_SETTING_KEYS
from omnibci.application.commands.acquisition_commands import StartStreamCommand
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.plugin_profiles import ProfileRevisionConflict

from omnibci.shared.events import EventType, Event
from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.ports.engine_port import (
    EngineFactory,
    EngineHandle,
    EnginePort,
    STIMULATION_PEER,
)
from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import (
    GetEngineStatusQuery,
    EngineStatusResult,
)

if TYPE_CHECKING:
    from omnibci.infrastructure.actuation.process_manager import ActuationProcessManager

logger = logging.getLogger(__name__)


class NeuroEngineService(EnginePort):
    """
    Thin application-service shell around NeuroEngine.

    Lifecycle:
        startup()  — called once during API server startup
        shutdown() — called once during API server shutdown

    All public methods correspond 1-to-1 with domain commands / queries so
    that routers stay free of business logic.
    """

    _STARTUP_TIMEOUT_S = 10.0
    _SHUTDOWN_TIMEOUT_S = 5.0

    def __init__(
        self,
        engine_factory: EngineFactory,
        actuation_process_manager: "ActuationProcessManager | None" = None,
        *,
        snapshot_provider: Callable[[], dict] | None = None,
        admission: RuntimeAdmission | None = None,
        stimulation_process_manager=None,
    ) -> None:
        # The composition root supplies a fresh graph factory and, for managed
        # configuration, the snapshot to pin before constructing any worker.
        self._engine_factory = engine_factory
        self._admission = admission or RuntimeAdmission()
        self._snapshot_provider = snapshot_provider
        self._graph_snapshot: dict | None = None
        self._applied_snapshot: dict | None = None
        self._actuation_manager = actuation_process_manager
        self._stimulation_manager = stimulation_process_manager
        self._actuation_active = False
        self._acquisition = None
        self._commands = None
        self._signal_setup = None
        self._module_snapshot_provider = None
        self._engine: Optional[EngineHandle] = None
        self._initialized = False
        self._engine_thread: Optional[Thread] = None
        self._running = False
        # Runtime command admission is intentionally separate from ownership.
        # A timed-out stop keeps _running=True so a second graph cannot start,
        # while this flag is already False so no new events enter a graph that
        # is tearing down.
        self._accepting_commands = False
        # mp.Process is single-use and NeuroEngine._stop() closes its queues,
        # so after a stop the whole engine graph must be rebuilt before the
        # next start.  Bus subscriptions made via subscribe() are recorded so
        # they can be replayed onto the fresh EventBus after a rebuild.
        self._needs_rebuild = False
        self._restart_blocked = False
        self._subscriptions: list = []
        # API requests can issue start/stop concurrently.  One lock owns the
        # full thread/process transition so STARTING cannot be mistaken for an
        # idle service and started a second time.
        self._lifecycle_lock = asyncio.Lock()
        self._configuration_lock = RLock()
        self._transitioning = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def read_acquisition_preview(self, capture_id: str | None, sequence_id: int | None = None) -> dict:
        """Read the current graph's independent cache without consuming signal data."""
        with self._configuration_lock:
            if not self.is_engine_ready or self._engine is None:
                raise RuntimeError("Engine is not ready")
            return self._engine.read_acquisition_preview(capture_id, sequence_id)

    async def startup(self) -> None:
        """Allow listener registration; build the graph at the actual start."""
        self._initialized = True
        logger.info("NeuroEngineService initialised")

    async def shutdown(self) -> None:
        """Stop everything cleanly."""
        if self._running or self._restart_blocked or (
            self._engine_thread is not None and self._engine_thread.is_alive()
        ):
            await self.handle_stop_engine(StopEngineCommand())

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    @admitted
    async def handle_start_engine(self, cmd: StartEngineCommand) -> None:
        async with self._lifecycle_lock:
            with self._configuration_transition():
                await self._start_engine_locked(cmd)

    async def _start_engine_locked(
        self, cmd: StartEngineCommand, *, settings_snapshot: dict | None = None,
    ) -> None:
        if self._running:
            logger.warning("Engine already running — ignoring StartEngineCommand")
            return
        if self._restart_blocked:
            raise RuntimeError(
                "Previous NeuroEngine graph did not release all resources; "
                "restart is blocked"
            )
        if not self.is_initialized:
            raise RuntimeError("NeuroEngineService not initialised; call startup() first")

        if self._engine is None or self._needs_rebuild:
            # Rebuild the engine graph (fresh workers, queues, bus, gateway),
            # then replay result listeners and the WS broadcaster onto
            # the new EventBus — they were registered on the discarded one.
            snapshot = settings_snapshot
            if snapshot is None and self._snapshot_provider is not None:
                snapshot = self._snapshot_provider()
            if snapshot is None:
                if self._snapshot_provider is not None:
                    raise ValueError("snapshot_provider must return a configuration dictionary")
                self._engine = self._engine_factory()
                self._graph_snapshot = None
            else:
                if not isinstance(snapshot, dict):
                    raise ValueError("settings_snapshot must be a configuration dictionary")
                snapshot = deepcopy(snapshot)
                snapshot["CONFIG.generation"] = str(uuid4())
                self._engine = self._engine_factory(deepcopy(snapshot))
                self._graph_snapshot = snapshot
            for event_type, handler in self._subscriptions:
                self._engine.event_bus.register_event_listener(event_type, handler)
            self._needs_rebuild = False
            self._restart_blocked = False
            logger.info(
                "NeuroEngine rebuilt (%d listeners replayed)", len(self._subscriptions)
            )

        # NeuroEngine.run() calls asyncio.run() internally, so we spin it in a
        # thread.  Starting the OS thread is not readiness: wait for the engine's
        # explicit barrier so parent-loop IPC/listener bind failures reach this
        # caller. Output modules acknowledge their own readiness when used.
        self._engine_thread = Thread(target=self._run_engine, daemon=True, name="neuro-engine")
        self._engine_thread.start()
        try:
            await asyncio.to_thread(
                self._engine.wait_until_started,
                self._STARTUP_TIMEOUT_S,
            )
        except BaseException as exc:
            self._engine.should_stop = True
            await asyncio.to_thread(
                self._engine_thread.join,
                self._SHUTDOWN_TIMEOUT_S,
            )
            # A confirmed-dead failed graph is single-use and must be rebuilt;
            # an unresponsive thread remains owned here and blocks restart.
            if not self._engine_thread.is_alive():
                if self._engine_shutdown_complete():
                    self._needs_rebuild = True
                else:
                    self._restart_blocked = True
                self._engine_thread = None
            else:
                self._restart_blocked = True
            logger.exception("NeuroEngine failed to start")
            raise

        self._running = True
        with self._configuration_lock:
            self._applied_snapshot = deepcopy(self._graph_snapshot)
            self._accepting_commands = True
        logger.info("NeuroEngine started")

    def configure_modules(self, acquisition, commands, signal_setup, snapshot_provider):
        """Wire module-specific preparation after the application services exist."""
        self._acquisition = acquisition
        self._commands = commands
        self._signal_setup = signal_setup
        self._module_snapshot_provider = snapshot_provider

    def is_module_running(self, module: str) -> bool:
        if not self.is_engine_ready:
            return False
        if module == "actuation":
            return self._actuation_active and (self.actuation_process_alive or self.is_peer_connected(module))
        if module == "stimulation":
            return self.is_peer_connected(module)
        if module not in ("acquisition", "processing"):
            raise ValueError(f"Unknown module: {module}")
        return self._engine.is_module_running(module)

    def is_command_ready(self, event_type) -> bool:
        if event_type in {
            EventType.ConnectDataProvider, EventType.DisconnectDataProvider,
            EventType.GetSourceCheckpoint, EventType.RestoreSourceCheckpoint, EventType.GetSourceMetadata,
            EventType.StartStream, EventType.StopStream, EventType.StopAndDrain,
            EventType.StartAcquisitionPreview, EventType.StopAcquisitionPreview, EventType.GetAcquisitionPreview,
        }:
            return self.is_module_running("acquisition")
        if event_type in {EventType.StageProcessingConfiguration, EventType.CancelProcessingConfiguration,
                          EventType.PrepareSignalProcessing}:
            return self.is_module_running("processing")
        return self.is_engine_ready

    async def start_module(self, module: str) -> None:
        async with self.runtime_transition():
            if self._admission.recovery_required:
                raise ProfileRevisionConflict("Runtime requires recovery; stop the engine before starting a module")
            if not self.is_engine_ready:
                raise RuntimeError("Start the engine before starting a module")
            if self.is_module_running(module):
                return
            snapshot = self.applied_snapshot or {}
            if self._module_snapshot_provider is not None:
                selected = self._module_snapshot_provider(module)
                if selected["CONFIG.system_revision"] != snapshot["CONFIG.system_revision"]:
                    raise RuntimeError("Apply the current system settings before starting a module")
                keys = SECTION_SETTING_KEYS[module]
                snapshot = {key: value for key, value in snapshot.items()
                            if key not in keys and not (module == "acquisition" and key.startswith("ACQUISITION.") and key != "ACQUISITION.trigger")}
                snapshot.update({key: value for key, value in selected.items()
                                 if key in keys or module == "acquisition" and key.startswith("ACQUISITION.") and key != "ACQUISITION.trigger"})
                snapshot["PLUGINS.bindings"] = [item for item in snapshot.get("PLUGINS.bindings", ())
                                                if not item["slot"].startswith(f"{module}.")]
                snapshot["PLUGINS.bindings"].extend(item for item in selected.get("PLUGINS.bindings", ())
                                                   if item["slot"].startswith(f"{module}."))
                snapshot["CONFIG.module_revisions"][module] = selected["CONFIG.module_revisions"][module]
            state = await self._pause_reading() if module == "processing" else None
            try:
                await self.start_module_for_configuration(module, snapshot)
                self.acknowledge_configuration(snapshot)
            except BaseException:
                if state is not None and not self.is_module_running(module):
                    await self._resume_reading(state)
                raise
            if state is not None:
                await self._resume_reading(state)

    async def stop_module(self, module: str) -> None:
        async with self.runtime_transition():
            if not self.is_engine_ready:
                return
            if module not in ("acquisition", "processing", "actuation"):
                raise ValueError(f"Unknown module: {module}")
            state = (await self._pause_reading()
                     if module in ("acquisition", "processing") and self.is_module_running(module) else None)
            await self.stop_module_for_configuration(module)
            if module == "processing" and state is not None:
                await self._resume_reading(state)

    async def _pause_reading(self):
        if self._acquisition is None or not self.is_module_running("acquisition"):
            return None
        streaming = self._acquisition.handle_get_status(GetAcquisitionStatusQuery()).streaming
        await asyncio.to_thread(self._acquisition.handle_stop_and_drain)
        if not streaming:
            return False, None
        result = await asyncio.to_thread(self._commands.request, EventType.GetSourceCheckpoint,
                                        self._source_identity())
        data = result.data
        self._check_module_receipt(data, "acquisition")
        checkpoint = data.get("checkpoint") if data.get("supported") is True else None
        if checkpoint is None and data.get("resume_without_checkpoint_safe") is not True:
            raise RuntimeError("Source is paused because it does not support resuming after a module change")
        return True, checkpoint

    async def _resume_reading(self, state):
        if state is None or not state[0]:
            return
        if state[1] is not None:
            result = await asyncio.to_thread(self._commands.request, EventType.RestoreSourceCheckpoint,
                                            {**self._source_identity(), "checkpoint": state[1]})
            self._check_module_receipt(result.data, "acquisition", "restored")
        await asyncio.to_thread(self._acquisition.handle_start_stream, StartStreamCommand())

    def _source_identity(self):
        snapshot = self.applied_snapshot
        return {"generation": self.generation, "system_revision": snapshot["CONFIG.system_revision"],
                "expected_revision": snapshot["CONFIG.module_revisions"]["acquisition"]}

    def _check_module_receipt(self, data, module, *flags):
        snapshot = self.applied_snapshot
        revision_key = "acquisition_revision" if module == "acquisition" else "module_revision"
        if (not isinstance(data, dict) or data.get("generation") != self.generation
                or data.get("system_revision") != snapshot["CONFIG.system_revision"]
                or data.get(revision_key) != snapshot["CONFIG.module_revisions"][module]
                or any(data.get(flag) is not True for flag in flags)):
            raise RuntimeError(f"{module.title()} did not confirm the requested boundary")

    async def start_module_for_configuration(self, module, snapshot):
        if not self._admission.is_transition_owner or not self.is_engine_ready:
            raise RuntimeError("A running transition must own module startup")
        if module == "actuation":
            if snapshot.get("ACTUATION.auto_spawn", True):
                if self._actuation_manager is None:
                    raise RuntimeError("No managed Actuation process is available")
                await asyncio.to_thread(self._actuation_manager.start, settings_snapshot=deepcopy(snapshot))
            else:
                if not self.is_peer_connected("actuation"):
                    raise RuntimeError("Connect the external Actuation node before starting it")
                result = await asyncio.to_thread(self._commands.request, EventType.ResumeActuation)
                self._check_module_receipt(result.data, "actuation", "resumed")
            await self._engine.set_actuation_active(True)
            self._actuation_active = True
        else:
            await self._engine.start_module(module, deepcopy(snapshot))
            if module == "processing" and self._signal_setup is not None:
                self._signal_setup.reset()

    async def stop_module_for_configuration(self, module):
        if not self._admission.is_transition_owner:
            raise RuntimeError("A runtime transition must own module shutdown")
        if module == "actuation":
            await self._engine.set_actuation_active(False)
            if self._actuation_active and self.is_peer_connected("actuation"):
                result = await asyncio.to_thread(self._commands.request, EventType.SettleActuation)
                self._check_module_receipt(result.data, "actuation", "paused", "execution_complete", "outputs_settled")
            if self._actuation_manager is not None:
                await asyncio.to_thread(self._actuation_manager.stop)
            self._actuation_active = False
        else:
            await self._engine.stop_module(module)
            if module == "acquisition" and self._acquisition is not None:
                self._acquisition.reset()
            if module == "processing" and self._signal_setup is not None:
                self._signal_setup.reset()

    @admitted(recovery=True)
    async def handle_stop_engine(self, cmd: StopEngineCommand) -> None:
        async with self._lifecycle_lock:
            with self._configuration_transition():
                await self._stop_engine_locked(cmd)
                if not self._restart_blocked:
                    self._admission.clear_quarantine()

    @contextmanager
    def _configuration_transition(self):
        # Mark ownership before snapshot resolution or any await. The short
        # threading lock also excludes synchronous config API writes.
        with self._configuration_lock:
            self._transitioning = True
        try:
            yield
        finally:
            with self._configuration_lock:
                self._transitioning = False

    @contextmanager
    def configuration_change(self, requires_idle: bool):
        """Keep an effective configuration write atomic with engine admission."""
        with self._admission.command(), self._configuration_lock:
            if requires_idle and (self._transitioning or self._running or self._restart_blocked):
                raise ConfigProfileConflictError(
                    "Stop the engine before changing the effective acquisition configuration")
            yield

    def node_configuration_snapshot(self, selected_snapshot: Callable[[], dict]) -> tuple[str, dict]:
        """Publish one settled snapshot; never expose an in-flight or historical graph."""
        with self._admission.command(), self._configuration_lock:
            if self._transitioning:
                raise ConfigProfileConflictError("Engine transition in progress; retry after it completes")
            if self.is_engine_ready and self._applied_snapshot is not None:
                return "running", deepcopy(self._applied_snapshot)
            thread = self._engine_thread
            if self._running or self._restart_blocked or thread is not None and thread.is_alive():
                raise ConfigProfileConflictError("Runtime still owns resources; stop it before connecting nodes")
            return "selected", selected_snapshot()

    async def _stop_engine_locked(self, cmd: StopEngineCommand) -> None:
        # Close the synchronous dispatch gate before the first await.  Resource
        # ownership may remain _running=True until shutdown is confirmed (and
        # deliberately remains so after a timeout), but new commands must not
        # race that teardown.
        self._accepting_commands = False
        if self._engine is None:
            return
        if not self._running and not self._restart_blocked and (
            self._engine_thread is None or not self._engine_thread.is_alive()
        ):
            return
        manager_error = None
        if self._stimulation_manager is not None:
            try:
                await asyncio.to_thread(self._stimulation_manager.stop)
            except Exception as exc:
                manager_error = exc
                logger.exception("Stimulation process failed to stop")
        if self._actuation_manager is not None:
            try:
                # Stop the dial-out controller while the engine socket and bus
                # still exist, so its teardown/final feedback has a live IPC
                # boundary.  Engine cleanup proceeds even if this adapter fails.
                await asyncio.to_thread(self._actuation_manager.stop)
            except Exception as exc:
                manager_error = exc
                logger.exception("Actuation process failed to stop")
        self._engine.should_stop = True
        if self._engine_thread:
            await asyncio.to_thread(
                self._engine_thread.join,
                self._SHUTDOWN_TIMEOUT_S,
            )
            if self._engine_thread.is_alive():
                # Keep ownership and the running flag: advertising STOPPED here
                # would permit a second graph to contend for the same sockets
                # and devices.
                raise RuntimeError(
                    "NeuroEngine thread did not stop within "
                    f"{self._SHUTDOWN_TIMEOUT_S:g}s"
                )
        self._running = False
        self._actuation_active = False
        self._engine_thread = None
        if not self._engine_shutdown_complete():
            self._restart_blocked = True
            raise RuntimeError(
                "NeuroEngine stopped with unreleased resources; restart is blocked"
            )
        self._needs_rebuild = True
        self._restart_blocked = False
        if self._acquisition is not None:
            self._acquisition.reset()
        if self._signal_setup is not None:
            self._signal_setup.reset()
        logger.info("NeuroEngine stopped")
        if manager_error is not None:
            self._restart_blocked = True
            raise RuntimeError("Managed output process failed to stop") from manager_error

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    def handle_get_status(self, query: GetEngineStatusQuery) -> EngineStatusResult:
        if not self.is_engine_ready:
            # A stopped engine closes its multiprocessing.Process handles;
            # calling is_alive() on a closed handle raises ValueError.
            return EngineStatusResult(
                running=False,
                acquisition_alive=False,
                processor_alive=False,
                stimulation_connected=False,
            )
        return EngineStatusResult(
            running=True,
            acquisition_alive=self.is_module_running("acquisition"),
            processor_alive=self.is_module_running("processing"),
            stimulation_connected=self.is_peer_connected(STIMULATION_PEER),
            actuation_alive=self.is_module_running("actuation"),
            actuation_connected=self.is_peer_connected("actuation"),
        )

    # ------------------------------------------------------------------
    # EnginePort implementation
    # ------------------------------------------------------------------

    def dispatch(self, event: Event) -> None:
        """
        Put an event onto the running engine's bus.

        This is the single seam other bounded-context services use instead of
        reaching into ``_engine.event_bus`` directly.
        """
        with self._admission.command():
            self._require_engine()
            if not self.is_engine_ready:
                raise RuntimeError("NeuroEngine is not running")
            self._engine.event_bus.put_event(event)

    @property
    def admission(self):
        return self._admission

    @property
    def restart_blocked(self) -> bool:
        return self._restart_blocked

    @asynccontextmanager
    async def runtime_transition(self):
        with self._admission.transition():
            async with self._lifecycle_lock:
                with self._configuration_transition():
                    yield

    async def stop_for_configuration(self):
        if not self._admission.is_transition_owner:
            raise RuntimeError("A runtime configuration transition must own this operation")
        await self._stop_engine_locked(StopEngineCommand())

    async def start_for_configuration(self, snapshot: dict):
        if not self._admission.is_transition_owner:
            raise RuntimeError("A runtime configuration transition must own this operation")
        await self._start_engine_locked(StartEngineCommand(), settings_snapshot=snapshot)

    def acknowledge_configuration(self, snapshot: dict):
        if not self._admission.is_transition_owner or not self.is_engine_ready:
            raise RuntimeError("A running configuration transition must own this operation")
        with self._configuration_lock:
            snapshot = deepcopy(snapshot)
            snapshot["CONFIG.generation"] = self.generation
            self._applied_snapshot = snapshot
            self._graph_snapshot = deepcopy(snapshot)

    async def stop_actuation_for_configuration(self):
        """Release an output whose settlement was confirmed by the coordinator."""
        if not self._admission.is_transition_owner:
            raise RuntimeError("A runtime transition must own output replacement")
        await self._engine.set_actuation_active(False)
        if self._actuation_manager is not None:
            await asyncio.to_thread(self._actuation_manager.stop)
        self._actuation_active = False

    async def reconfigure_output_modules(self, snapshot: dict, modules: tuple[str, ...]):
        if not self._admission.is_transition_owner or not self.is_engine_ready:
            raise RuntimeError("A running configuration transition must own output replacement")
        await self._engine.reconfigure_output_modules(deepcopy(snapshot), modules)

    @property
    def is_initialized(self) -> bool:
        """True once the service can accept composition-time subscriptions."""
        return self._initialized or self._engine is not None

    @property
    def is_engine_ready(self) -> bool:
        """Foundation readiness is independent of business module processes."""
        thread = self._engine_thread
        return bool(
            self._engine is not None
            and self._running
            and self._accepting_commands
            and thread is not None
            and thread.is_alive()
        )

    @property
    def actuation_process_alive(self) -> bool:
        """Expose managed output ownership independently of the acquisition graph."""
        return bool(self._actuation_manager is not None and self._actuation_manager.is_alive)

    @property
    def applied_snapshot(self) -> dict | None:
        """Return a detached copy; saved edits cannot mutate a running graph."""
        with self._configuration_lock:
            return deepcopy(self._applied_snapshot)

    @property
    def runtime_package_snapshots(self) -> tuple[dict, ...]:
        """Retain both acknowledged and attempted graph packages until ownership ends."""
        with self._configuration_lock:
            thread = self._engine_thread
            if not (self._running or self._restart_blocked or thread is not None and thread.is_alive()):
                return ()
            return tuple(deepcopy(snapshot) for snapshot in (self._applied_snapshot, self._graph_snapshot)
                         if snapshot is not None)

    @property
    def applied_revisions(self) -> dict[str, str] | None:
        with self._configuration_lock:
            if self._applied_snapshot is None:
                return None
            return {**self._applied_snapshot.get("CONFIG.module_revisions", {}),
                    "system": self._applied_snapshot.get("CONFIG.system_revision")}

    @property
    def generation(self) -> str | None:
        with self._configuration_lock:
            return (
                self._applied_snapshot.get("CONFIG.generation")
                if self._applied_snapshot is not None else None
            )

    def is_peer_connected(self, peer_name: str) -> bool:
        """True when the named peer's transport has an active connection.

        Returns False if the engine is not running or the peer doesn't exist.
        Safe to call at any time — never raises.
        """
        if not self.is_engine_ready:
            return False
        try:
            return self._engine.peer_gateway.peer(peer_name).is_connected
        except Exception:
            return False

    def subscribe(self, event_type, handler) -> None:
        """
        Register a listener on the engine's event bus.

        Exposed so the composition root can wire EventBus → broadcaster /
        read-model listeners without reaching into ``_engine.event_bus``.
        Subscriptions are recorded so they survive an engine rebuild after a
        stop (see handle_start_engine).
        """
        if not self.is_initialized:
            raise RuntimeError("NeuroEngineService not initialised")
        self._subscriptions.append((event_type, handler))
        if self._engine is not None:
            self._engine.event_bus.register_event_listener(event_type, handler)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_engine(self) -> None:
        if self._engine is None:
            raise RuntimeError("NeuroEngineService not initialised")

    def _run_engine(self):
        try:
            self._engine.run()
        except BaseException as exc:
            # Startup failures also cross the explicit readiness barrier. Keep
            # the thread exception contained so recovery owns the failed graph.
            if self._running:
                self._admission.quarantine(str(exc))
            logger.exception("NeuroEngine execution failed")
        finally:
            self._accepting_commands = False

    def _engine_shutdown_complete(self) -> bool:
        """Read the optional concrete lifecycle signal without trusting mocks."""
        value = getattr(self._engine, "shutdown_complete", True)
        return value if isinstance(value, bool) else True
