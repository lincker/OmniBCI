"""
NeuroEngineService — Application Service.

Orchestrates NeuroEngine lifecycle and exposes it to the API layer via
command / query objects.  The API layer never touches NeuroEngine directly.
"""

import asyncio
import inspect
import logging
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from threading import RLock, Thread
from typing import Callable, Optional
from uuid import uuid4

from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.configuration.definitions import merge_module_settings
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.command_contracts import command_contract, command_event
from omnibci.application.commands.acquisition_commands import StartStreamCommand
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery

from omnibci.shared.events import Event, EventType
from omnibci.application.ports.engine_port import (
    EngineFactory,
    EngineHandle,
    EnginePort,
)
from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
)
from omnibci.application.queries.engine_queries import (
    GetEngineStatusQuery,
    EngineStatusResult,
)

logger = logging.getLogger(__name__)


class NeuroEngineService(EnginePort):
    """
    Application service coordinating NeuroEngine lifecycle, independent modules,
    admission control, and runtime snapshots.

    Lifecycle:
        startup()  — called once during API server startup
        shutdown() — called once during API server shutdown
    """

    _STARTUP_TIMEOUT_S = 10.0
    _SHUTDOWN_TIMEOUT_S = 5.0

    def __init__(
        self,
        engine_factory: EngineFactory,
        *,
        snapshot_provider: Callable[[], dict] | None = None,
        admission: RuntimeAdmission | None = None,
        plugin_preparer: Callable[[dict, tuple[str, ...]], dict] | None = None,
    ) -> None:
        # The composition root supplies a fresh graph factory and, for managed
        # configuration, the snapshot to pin before constructing any worker.
        self._engine_factory = engine_factory
        self._admission = admission or RuntimeAdmission()
        self._snapshot_provider = snapshot_provider
        self._plugin_preparer = plugin_preparer
        self._graph_snapshot: dict | None = None
        self._applied_snapshot: dict | None = None
        self._actuation = None
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

    def on_stimulation_ack(self, event: Event) -> None:
        with self._configuration_lock:
            if event.type == EventType.StimulationAck and self.is_engine_ready:
                self._engine.dispatch(event)

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
        await self._owned_lifecycle("start", "engine", lambda: self._start_engine_locked(cmd))

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
                snapshot = {"PLUGINS.bindings": []}
            if not isinstance(snapshot, dict):
                raise ValueError("settings_snapshot must be a configuration dictionary")
            snapshot = deepcopy(snapshot)
            snapshot["CONFIG.generation"] = str(uuid4())
            self._engine = self._engine_factory(deepcopy(snapshot))
            self._graph_snapshot = snapshot
            for event_type, handler in self._subscriptions:
                self._engine.subscribe(event_type, handler)
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
            self._engine.request_stop()
            await asyncio.to_thread(
                self._engine_thread.join,
                self._SHUTDOWN_TIMEOUT_S,
            )
            # A confirmed-dead failed graph is single-use and must be rebuilt;
            # an unresponsive thread remains owned here and blocks restart.
            if not self._engine_thread.is_alive():
                if self._engine.shutdown_complete is True:
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

    def configure_modules(self, acquisition, commands, signal_setup, snapshot_provider,
                          *, stimulation=None, actuation=None):
        """Wire module-specific preparation after the application services exist."""
        self._acquisition = acquisition
        self._commands = commands
        self._actuation = actuation
        self._signal_setup = signal_setup
        self._module_snapshot_provider = snapshot_provider
        self._stimulation = stimulation

    def is_module_running(self, module: str) -> bool:
        if module == "stimulation":
            return bool(self._stimulation and self._stimulation.is_active)
        if module == "actuation":
            return bool(self._actuation and self._actuation.is_active)
        if not self.is_engine_ready:
            return False
        if module not in ("acquisition", "processing"):
            raise ValueError(f"Unknown module: {module}")
        return self._engine.is_module_running(module)

    def is_command_ready(self, event_type) -> bool:
        owner = command_contract(event_type).owner
        if owner in ("acquisition", "processing"):
            return self.is_module_running(owner)
        return self.is_engine_ready

    def selected_module_snapshot(self, module):
        snapshot = self.applied_snapshot or {}
        if self._module_snapshot_provider is not None:
            selected = self._module_snapshot_provider(module)
            if selected["CONFIG.engine_revision"] != snapshot.get("CONFIG.engine_revision"):
                raise RuntimeError("Apply the current Engine settings before starting a module")
            snapshot = merge_module_settings(snapshot, selected, (module,))
        return self.prepare_plugins(snapshot, (module,))

    def prepare_plugins(self, snapshot: dict, modules) -> dict:
        """Prepare only modules executed locally; external nodes resolve their own files."""
        local = tuple(module for module in modules if module not in ('stimulation', 'actuation')
                      or snapshot.get(f'{module.upper()}.auto_spawn', True))
        snapshot = deepcopy(snapshot)
        if self._plugin_preparer is None:
            if any(binding.get('slot', '').split('.', 1)[0] in local
                   for binding in snapshot.get('PLUGINS.bindings', ())):
                raise RuntimeError('Plugin artifact preparation is not configured')
            return snapshot
        return self._plugin_preparer(snapshot, local)

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
            recovery = operation == "prepare" or operation == "start" and scope == "engine"
            if operation != "stop" and "engine" in self._admission.recovery_status:
                raise ProfileRevisionConflict("Foundation requires recovery; stop it before restarting")
            reservation = (None if operation == "stop" else
                           self._admission.reserve_transition(scope, recovery=recovery))

            async def run():
                if operation == "stop":
                    async with self._admission.stop_transition(scope):
                        try:
                            await action()
                        except BaseException as exc:
                            if scope != "engine" or self._running or self._restart_blocked:
                                self._admission.quarantine(str(exc) or "Module stop was interrupted", scope)
                            else:
                                self._admission.clear_quarantine("engine")
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
        if module not in ("acquisition", "processing"):
            raise ValueError(f"Unknown module: {module}")
        async def stop():
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
            if module == "processing":
                drain = getattr(self._engine, "drain_abandoned_processing_queue", None)
                dirty = getattr(self._engine, "processing_queue_dirty", False) is True
                if inspect.iscoroutinefunction(drain) and (boundary_error is not None or dirty):
                    if not await drain():
                        error = RuntimeError(
                            "Processing input queue boundary could not be confirmed; stop and restart the Engine"
                        )
                        if boundary_error is not None:
                            boundary_error.add_note(str(error))
                            raise boundary_error
                        raise error
            if boundary_error is not None:
                if module == "processing":
                    raise boundary_error
                logger.warning("Acquisition stopped without a confirmed processing drain: %s", boundary_error)
        await self._owned_lifecycle("stop", module, stop)

    async def start_module_for_configuration(self, module, snapshot):
        if (not self._admission.is_transition_owner or not self._admission.owns(module)
                or not self.is_engine_ready):
            raise RuntimeError("A running transition must own module startup")
        await self._engine.start_module(module, deepcopy(snapshot))
        if module == "processing" and self._signal_setup is not None:
            self._signal_setup.reset()

    async def stop_module_for_configuration(self, module):
        if not self._admission.is_transition_owner or not self._admission.owns(module):
            raise RuntimeError("A runtime transition must own module shutdown")
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
        await self._owned_lifecycle("stop", "engine", stop)

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

    async def _stop_engine_locked(self, cmd: StopEngineCommand) -> None:
        # Stop admission excludes new commands while the owned cleanup still
        # drains acquisition before disposing the graph.
        if self._engine is None:
            return
        if not self._running and not self._restart_blocked and (
            self._engine_thread is None or not self._engine_thread.is_alive()
        ):
            return
        if self._acquisition is not None and self.is_module_running("acquisition"):
            try:
                if self._acquisition.is_streaming:
                    await asyncio.to_thread(self._acquisition.handle_stop_and_drain)
            except Exception:
                logger.exception("Acquisition failed to drain before foundation shutdown")
        self._accepting_commands = False
        self._engine.request_stop()
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
        if self._engine.shutdown_complete is not True:
            reconcile = getattr(self._engine, "reconcile_shutdown", None)
            if callable(reconcile):
                try:
                    await asyncio.to_thread(reconcile)
                except Exception:
                    logger.exception("NeuroEngine shutdown reconciliation failed")
        if self._engine.shutdown_complete is not True:
            self._restart_blocked = True
            raise RuntimeError(
                "NeuroEngine stopped with unreleased resources; restart is blocked"
            )
        self._needs_rebuild = True
        self._restart_blocked = False
        self._admission.clear_quarantine(("acquisition", "processing"))
        if self._acquisition is not None:
            self._acquisition.reset()
        if self._signal_setup is not None:
            self._signal_setup.reset()
        logger.info("NeuroEngine stopped")

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
                stimulation_connected=self.is_peer_connected("stimulation"),
                actuation_connected=self.is_peer_connected("actuation"),
            )
        return EngineStatusResult(
            running=True,
            acquisition_alive=self.is_module_running("acquisition"),
            processor_alive=self.is_module_running("processing"),
            stimulation_connected=bool(self._stimulation and self._stimulation.is_connected),
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
        event = command_event(event)
        with self._admission.command(command_contract(event.type).scopes):
            if not self.is_engine_ready:
                raise RuntimeError("NeuroEngine is not running")
            self._engine.dispatch(event)

    @property
    def admission(self):
        return self._admission

    @property
    def restart_blocked(self) -> bool:
        return self._restart_blocked

    @asynccontextmanager
    async def runtime_transition(self, scopes=None, *, reservation=None):
        with self._admission.transition(scopes, reservation=reservation):
            if self._admission.owns("engine"):
                async with self._lifecycle_lock:
                    with self._configuration_transition():
                        yield
            else:
                yield

    async def stop_for_configuration(self):
        if not self._admission.is_transition_owner or not self._admission.owns("engine"):
            raise RuntimeError("A runtime configuration transition must own this operation")
        await self._stop_engine_locked(StopEngineCommand())

    async def start_for_configuration(self, snapshot: dict):
        if not self._admission.is_transition_owner or not self._admission.owns("engine"):
            raise RuntimeError("A runtime configuration transition must own this operation")
        await self._start_engine_locked(StartEngineCommand(), settings_snapshot=snapshot)

    def acknowledge_configuration(self, snapshot: dict, *, modules=None):
        scopes = "engine" if modules is None else modules
        if not self._admission.is_transition_owner or not self._admission.owns(scopes) or not self.is_engine_ready:
            raise RuntimeError("A running configuration transition must own this operation")
        with self._configuration_lock:
            snapshot = (deepcopy(snapshot) if modules is None else
                        merge_module_settings(self._applied_snapshot or {}, snapshot, modules))
            snapshot["CONFIG.generation"] = self.generation
            self._applied_snapshot = snapshot
            self._graph_snapshot = deepcopy(snapshot)

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
        return bool(self._actuation and self._actuation.process_alive)

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
                    "engine": self._applied_snapshot.get("CONFIG.engine_revision")}

    @property
    def generation(self) -> str | None:
        with self._configuration_lock:
            return (
                self._applied_snapshot.get("CONFIG.generation")
                if self._applied_snapshot is not None else None
            )

    def is_peer_connected(self, peer_name: str) -> bool:
        owner = {"stimulation": self._stimulation, "actuation": self._actuation}.get(peer_name)
        return bool(owner and owner.is_connected)

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
            self._engine.subscribe(event_type, handler)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

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
