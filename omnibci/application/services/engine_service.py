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

from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.activation_settings import merge_module_settings
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.command_result_service import command_scopes
from omnibci.application.services.actuation_service import ActuationAppService
from omnibci.application.commands.stimulation_commands import StopStimulationCommand
from omnibci.application.commands.acquisition_commands import StartStreamCommand
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery

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
        self._actuation = ActuationAppService(self, actuation_process_manager)
        self._stimulation_manager = stimulation_process_manager
        self._stimulation = None
        self._acquisition = None
        self._commands = None
        self._signal_setup = None
        self._module_snapshot_provider = None
        self._lifecycle_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._recovery_snapshots: dict[str, dict] = {}
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
        await self.handle_stop_engine(StopEngineCommand())

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def handle_start_engine(self, cmd: StartEngineCommand) -> None:
        await self._owned_lifecycle("start", "system", lambda: self._start_engine_locked(cmd))

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

    def configure_modules(self, acquisition, commands, signal_setup, snapshot_provider, *, stimulation=None):
        """Wire module-specific preparation after the application services exist."""
        self._acquisition = acquisition
        self._commands = commands
        self._actuation.commands = commands
        self._signal_setup = signal_setup
        self._module_snapshot_provider = snapshot_provider
        self._stimulation = stimulation

    def is_module_running(self, module: str) -> bool:
        if not self.is_engine_ready:
            return False
        if module == "actuation":
            return self._actuation.active and (self.actuation_process_alive or self.is_peer_connected(module))
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
            EventType.AttachProcessingInput, EventType.DetachProcessingInput,
        }:
            return self.is_module_running("acquisition")
        if event_type in {EventType.StageProcessingConfiguration, EventType.CancelProcessingConfiguration,
                          EventType.PrepareSignalProcessing, EventType.ResetProcessingInput}:
            return self.is_module_running("processing")
        return self.is_engine_ready

    def selected_module_snapshot(self, module):
        snapshot = self.applied_snapshot or {}
        if self._module_snapshot_provider is not None:
            selected = self._module_snapshot_provider(module)
            if selected["CONFIG.system_revision"] != snapshot.get("CONFIG.system_revision"):
                raise RuntimeError("Apply the current system settings before starting a module")
            snapshot = merge_module_settings(snapshot, selected, (module,))
        return snapshot

    async def start_reading(self, *, expected_revision, generation, connection_id):
        """Validate the connection inside the same ownership interval as Start."""
        pending = self._lifecycle_tasks.get(("read", "acquisition"))
        if pending is not None and not pending.done():
            raise ProfileRevisionConflict("A reading request is already in progress")
        async def start():
            if not self.is_engine_ready or self._acquisition is None:
                raise RuntimeError("Start the engine and connect the source before reading")
            snapshot = self.applied_snapshot or {}
            source = self._acquisition.handle_get_status(GetAcquisitionStatusQuery())
            if (snapshot.get("CONFIG.generation") != generation
                    or snapshot.get("CONFIG.module_revisions", {}).get("acquisition") != expected_revision
                    or not source.connected or source.connection_id != connection_id):
                raise ProfileRevisionConflict("Source connection changed; refresh before reading")
            await asyncio.to_thread(self._acquisition.handle_start_stream, StartStreamCommand(),
                                    prepare_processing=False)
        await self._owned_lifecycle("read", "acquisition", start,
                                    after_transition=lambda: asyncio.to_thread(self._acquisition.prepare_processing))

    async def _owned_lifecycle(self, operation, scope, action, *, after_transition=None):
        key = operation, scope
        task = self._lifecycle_tasks.get(key)
        if task is None or task.done():
            recovery = operation == "prepare" or operation == "start" and scope == "system"
            if operation != "stop" and "system" in self._admission.recovery_status:
                raise ProfileRevisionConflict("Foundation requires recovery; stop it before restarting")
            reservation = (None if operation == "stop" else
                           self._admission.reserve_transition(scope, recovery=recovery))

            async def run():
                if operation == "stop":
                    async with self._admission.stop_transition(scope):
                        try:
                            await action()
                        except BaseException as exc:
                            if scope != "system" or self._running or self._restart_blocked:
                                self._admission.quarantine(str(exc) or "Module stop was interrupted", scope)
                            else:
                                self._admission.clear_quarantine("system")
                            raise
                        self._admission.clear_quarantine(scope)
                        self._recovery_snapshots.pop(scope, None)
                else:
                    async with self.runtime_transition(scope, reservation=reservation):
                        await action()
                if after_transition is not None:
                    await after_transition()

            try:
                task = asyncio.create_task(run())
            except BaseException:
                if reservation is not None:
                    self._admission.release_reservation(reservation)
                raise
            self._lifecycle_tasks[key] = task
            # Retrieve failures even when every HTTP waiter has disconnected.
            task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
        await asyncio.shield(task)

    async def stop_module(self, module: str) -> None:
        if module not in ("acquisition", "processing", "actuation"):
            raise ValueError(f"Unknown module: {module}")
        async def stop():
            if module == "actuation":
                await self.stop_module_for_configuration(module)
                return
            if not self.is_engine_ready:
                if self._running or self._restart_blocked:
                    raise RuntimeError("Stop the unavailable engine to release its module resources")
                return
            boundary_error = None
            try:
                if module == "processing" and self._signal_setup is not None:
                    await asyncio.to_thread(self._signal_setup.detach)
                if module == "acquisition" and self._acquisition is not None and self.is_module_running(module):
                    await asyncio.to_thread(self._acquisition.handle_stop_and_drain)
            except Exception as exc:
                boundary_error = exc
                self._admission.quarantine(str(exc), "processing")
            try:
                await self.stop_module_for_configuration(module)
            except Exception as exc:
                if boundary_error is not None:
                    raise RuntimeError(f"Input boundary failed: {boundary_error}; {module} stop failed: {exc}") from exc
                raise
            if boundary_error is not None:
                if module == "processing":
                    raise boundary_error
                logger.warning("Acquisition stopped without a confirmed processing drain: %s", boundary_error)
        await self._owned_lifecycle("stop", module, stop)

    async def start_module_for_configuration(self, module, snapshot):
        if (not self._admission.is_transition_owner or not self._admission.owns(module)
                or not self.is_engine_ready):
            raise RuntimeError("A running transition must own module startup")
        if module == "actuation":
            await self._actuation.start(snapshot)
        else:
            await self._engine.start_module(module, deepcopy(snapshot))
            if module == "processing" and self._signal_setup is not None:
                self._signal_setup.reset()

    async def stop_module_for_configuration(self, module):
        if not self._admission.is_transition_owner or not self._admission.owns(module):
            raise RuntimeError("A runtime transition must own module shutdown")
        if module == "actuation":
            await self._actuation.stop()
        else:
            await self._engine.stop_module(module)
            if module == "acquisition" and self._acquisition is not None:
                self._acquisition.reset()
            if module == "processing" and self._signal_setup is not None:
                self._signal_setup.reset()

    async def handle_stop_engine(self, cmd: StopEngineCommand) -> None:
        async def stop():
            async with self._lifecycle_lock:
                with self._configuration_transition():
                    await self._stop_engine_locked(cmd)
        await self._owned_lifecycle("stop", "system", stop)

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
    def configuration_change(self):
        """Keep saved system setting writes atomic with engine admission."""
        with self._admission.command(), self._configuration_lock:
            yield

    def node_configuration_snapshot(self, selected_snapshot: Callable[[], dict], *, module=None) -> tuple[str, dict]:
        """Publish one settled snapshot; never expose an in-flight or historical graph."""
        with self._admission.command(module, recovery=module in ("stimulation", "actuation")), self._configuration_lock:
            if self._transitioning:
                raise ConfigProfileConflictError("Engine transition in progress; retry after it completes")
            if self._admission.requires_recovery(module):
                snapshot = self._output_snapshot(module)
                applied = self._applied_snapshot or {}
                if (not self.is_engine_ready or applied.get("CONFIG.module_revisions", {}).get(module)
                        != snapshot.get("CONFIG.module_revisions", {}).get(module)):
                    raise ProfileRevisionConflict("Prepare the retained external configuration before reconnecting")
            if self.is_engine_ready and self._applied_snapshot is not None:
                return "running", deepcopy(self._applied_snapshot)
            thread = self._engine_thread
            if self._running or self._restart_blocked or thread is not None and thread.is_alive():
                raise ConfigProfileConflictError("Runtime still owns resources; stop it before connecting nodes")
            return "selected", selected_snapshot()

    async def _stop_engine_locked(self, cmd: StopEngineCommand) -> None:
        # Stop admission excludes new commands while the owned cleanup still
        # uses the foundation to settle outputs and drain acquisition.
        if self._engine is None:
            return
        if not self._running and not self._restart_blocked and (
            self._engine_thread is None or not self._engine_thread.is_alive()
        ):
            return
        manager_errors = []
        local_cleanup_failed = False
        stimulation_error = None
        if self._stimulation is not None:
            try:
                if (self._stimulation.is_active or self.is_peer_connected("stimulation")
                        or "stimulation" in self._admission.recovery_status):
                    await asyncio.to_thread(self._stimulation.handle_stop_stimulation, StopStimulationCommand())
            except Exception as exc:
                stimulation_error = exc
                manager_errors.append(("Stimulation", exc))
                logger.exception("Stimulation failed to stop")
        if self._stimulation_manager is not None:
            try:
                await asyncio.to_thread(self._stimulation_manager.stop)
            except Exception as exc:
                local_cleanup_failed = True
                stimulation_error = exc
                manager_errors.append(("Stimulation process", exc))
                logger.exception("Stimulation process failed to stop")
        if stimulation_error is not None:
            self._admission.quarantine(str(stimulation_error), "stimulation")
            snapshot = self.applied_snapshot or {}
            if snapshot.get("CONFIG.module_revisions", {}).get("stimulation"):
                self._recovery_snapshots["stimulation"] = merge_module_settings({}, snapshot, ("stimulation",))
        elif self._stimulation is not None:
            self._admission.clear_quarantine("stimulation")
            self._recovery_snapshots.pop("stimulation", None)
        actuation_snapshot = self._actuation.owned_snapshot or self.applied_snapshot or {}
        try:
            await self._actuation.stop()
        except Exception as exc:
            self._admission.quarantine(str(exc), "actuation")
            if actuation_snapshot.get("CONFIG.module_revisions", {}).get("actuation"):
                self._recovery_snapshots["actuation"] = merge_module_settings({}, actuation_snapshot, ("actuation",))
            if (actuation_snapshot.get("ACTUATION.auto_spawn", True)
                    and (self._actuation.owned_snapshot is not None or self.actuation_process_alive)):
                local_cleanup_failed = True
            manager_errors.append(("Actuation", exc))
            logger.exception("Actuation failed to stop")
        else:
            self._admission.clear_quarantine("actuation")
            self._recovery_snapshots.pop("actuation", None)
        if self._acquisition is not None and self.is_module_running("acquisition"):
            try:
                if self._acquisition.is_streaming:
                    await asyncio.to_thread(self._acquisition.handle_stop_and_drain)
            except Exception as exc:
                logger.exception("Acquisition failed to drain before foundation shutdown")
        self._accepting_commands = False
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
        self._engine_thread = None
        if not self._engine_shutdown_complete():
            self._restart_blocked = True
            raise RuntimeError(
                "NeuroEngine stopped with unreleased resources; restart is blocked"
            )
        self._needs_rebuild = True
        self._restart_blocked = local_cleanup_failed
        self._admission.clear_quarantine(("acquisition", "processing"))
        if self._acquisition is not None:
            self._acquisition.reset()
        if self._signal_setup is not None:
            self._signal_setup.reset()
        logger.info("NeuroEngine stopped")
        if manager_errors:
            details = "; ".join(f"{module}: {error}" for module, error in manager_errors)
            raise RuntimeError(f"Module shutdown was not confirmed: {details}") from manager_errors[0][1]

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
        with self._admission.command(command_scopes(event.type)):
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
    async def runtime_transition(self, scopes=None, *, reservation=None):
        with self._admission.transition(scopes, reservation=reservation):
            if self._admission.owns("system"):
                async with self._lifecycle_lock:
                    with self._configuration_transition():
                        yield
            else:
                yield

    async def stop_for_configuration(self):
        if not self._admission.is_transition_owner or not self._admission.owns("system"):
            raise RuntimeError("A runtime configuration transition must own this operation")
        await self._stop_engine_locked(StopEngineCommand())

    async def start_for_configuration(self, snapshot: dict):
        if not self._admission.is_transition_owner or not self._admission.owns("system"):
            raise RuntimeError("A runtime configuration transition must own this operation")
        await self._start_engine_locked(StartEngineCommand(), settings_snapshot=snapshot)

    def acknowledge_configuration(self, snapshot: dict, *, modules=None):
        scopes = "system" if modules is None else modules
        if not self._admission.is_transition_owner or not self._admission.owns(scopes) or not self.is_engine_ready:
            raise RuntimeError("A running configuration transition must own this operation")
        with self._configuration_lock:
            snapshot = (deepcopy(snapshot) if modules is None else
                        merge_module_settings(self._applied_snapshot or {}, snapshot, modules))
            snapshot["CONFIG.generation"] = self.generation
            self._applied_snapshot = snapshot
            self._graph_snapshot = deepcopy(snapshot)

    async def prepare_output(self, module):
        if module not in ("stimulation", "actuation"):
            raise ValueError("Only output modules have external listeners")
        async def prepare():
            if not self.is_engine_ready:
                raise RuntimeError("Start the engine before preparing a module")
            snapshot = self._output_snapshot(module)
            await self._engine.ensure_output_ready(module, snapshot)
            self.acknowledge_configuration(snapshot, modules=(module,))
            if module == "actuation" and self._admission.requires_recovery(module):
                self._actuation.reconnect(snapshot)
        await self._owned_lifecycle("prepare", module, prepare)

    def _output_snapshot(self, module):
        if not self._admission.requires_recovery(module):
            return self.selected_module_snapshot(module)
        if "system" in self._admission.recovery_status:
            raise ProfileRevisionConflict("Foundation requires recovery before preparing an output")
        retained = (self._actuation.owned_snapshot if module == "actuation" else None) or self.applied_snapshot or {}
        if not retained.get("CONFIG.module_revisions", {}).get(module):
            retained = self._recovery_snapshots.get(module, {})
        if (retained.get(f"{module.upper()}.auto_spawn", True)
                or not retained.get("CONFIG.module_revisions", {}).get(module)):
            raise ProfileRevisionConflict("Only the retained external configuration can be prepared during recovery")
        self._recovery_snapshots[module] = merge_module_settings({}, retained, (module,))
        return merge_module_settings(self.applied_snapshot or {}, retained, (module,))

    def ensure_output_ready_sync(self, module, snapshot):
        if not self.is_engine_ready:
            raise RuntimeError("Start the engine before starting a module")
        self._engine.ensure_output_ready_sync(module, deepcopy(snapshot))

    async def ensure_output_ready(self, module, snapshot):
        if not self.is_engine_ready:
            raise RuntimeError("Start the engine before preparing a module")
        await self._engine.ensure_output_ready(module, deepcopy(snapshot))

    async def set_actuation_active(self, active):
        await self._engine.set_actuation_active(active)

    async def stop_actuation_for_configuration(self):
        """Release an output whose settlement was confirmed by the coordinator."""
        if not self._admission.is_transition_owner or not self._admission.owns("actuation"):
            raise RuntimeError("A running transition must own output replacement")
        await self._actuation.stop(settled=True)

    async def reconfigure_output_modules(self, snapshot: dict, modules: tuple[str, ...]):
        if (not self._admission.is_transition_owner or not self._admission.owns(modules)
                or not self.is_engine_ready):
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
        return self._actuation.process_alive

    @property
    def applied_snapshot(self) -> dict | None:
        """Return a detached copy; saved edits cannot mutate a running graph."""
        with self._configuration_lock:
            return deepcopy(self._applied_snapshot)

    @property
    def foundation_requires_cleanup(self) -> bool:
        """Retained external recovery references do not own the foundation."""
        with self._configuration_lock:
            thread = self._engine_thread
            return bool(self._running or self._restart_blocked or thread is not None and thread.is_alive())

    @property
    def runtime_package_snapshots(self) -> tuple[dict, ...]:
        """Retain both acknowledged and attempted graph packages until ownership ends."""
        with self._configuration_lock:
            snapshots = [snapshot for module, snapshot in self._recovery_snapshots.items()
                         if module in self._admission.recovery_status]
            if self.foundation_requires_cleanup:
                snapshots.extend(snapshot for snapshot in (self._applied_snapshot, self._graph_snapshot)
                                 if snapshot is not None)
            return tuple(deepcopy(snapshot) for snapshot in snapshots)

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

    def on_peer_delivery_fault(self, event: Event) -> None:
        """Keep output delivery failures scoped to the current channel owner."""
        fault = event.data
        if (not isinstance(fault, dict) or self._engine is None
                or fault.get("peer") not in {"stimulation", "actuation"}
                or not isinstance(fault.get("channel_id"), str)
                or not isinstance(fault.get("reason"), str)):
            return
        try:
            channel = self._engine.peer_gateway.peer(fault["peer"])
        except KeyError:
            return
        if channel.channel_id != fault["channel_id"]:
            return
        self._admission.quarantine(fault["reason"], fault["peer"])
        if self._commands is not None:
            self._commands.on_peer_delivery_fault(event)

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
