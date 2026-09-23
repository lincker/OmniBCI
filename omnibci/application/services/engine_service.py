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

from omnibci.shared.events import EventType, Event
from omnibci.application.configuration import ConfigProfileConflictError
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
        # Actuation has no user command (stimulation spawns on
        # handle_start_stimulation); its natural lifecycle hook is the
        # engine's. Injected by the composition root; None / auto_spawn=False
        # make both hooks no-ops. Duck-typed like StimulationAppService's
        # process manager — no infrastructure import at runtime.
        self._actuation_manager = actuation_process_manager
        self._stimulation_manager = stimulation_process_manager
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

    async def startup(self) -> None:
        """Allow listener registration; build the graph at the actual start."""
        self._initialized = True
        logger.info("NeuroEngineService initialised")

    async def shutdown(self) -> None:
        """Stop everything cleanly."""
        if self._running or (
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
            # then replay listeners (session read-model + WS broadcaster) onto
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
        # caller.  The managed Actuation child has its own READY/FAILED barrier
        # below because plugin/device setup happens in that subprocess.
        self._engine_thread = Thread(target=self._run_engine, daemon=True, name="neuro-engine")
        self._engine_thread.start()
        try:
            await asyncio.to_thread(
                self._engine.wait_until_started,
                self._STARTUP_TIMEOUT_S,
            )
        except BaseException:
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
        if self._actuation_manager is not None:
            try:
                # Listening sockets are now bound.  Managed mode returns only
                # after the child has connected and its Effector setup succeeds;
                # external/disabled mode remains an immediate no-op.
                if self._graph_snapshot is None:
                    await asyncio.to_thread(self._actuation_manager.start)
                else:
                    await asyncio.to_thread(
                        self._actuation_manager.start,
                        settings_snapshot=deepcopy(self._graph_snapshot),
                    )
            except BaseException:
                logger.exception("Actuation process failed to start; stopping engine")
                await self._stop_engine_locked(StopEngineCommand())
                raise
        with self._configuration_lock:
            self._applied_snapshot = deepcopy(self._graph_snapshot)
            self._accepting_commands = True
        logger.info("NeuroEngine started")

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
        self._engine_thread = None
        if not self._engine_shutdown_complete():
            self._restart_blocked = True
            raise RuntimeError(
                "NeuroEngine stopped with unreleased resources; restart is blocked"
            )
        self._needs_rebuild = True
        self._restart_blocked = False
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
            acquisition_alive=(
                self._engine.acquisition_worker.is_alive()
                if self._engine.acquisition_worker else False
            ),
            processor_alive=(
                self._engine.signal_processing_worker.is_alive()
                if self._engine.signal_processing_worker else False
            ),
            stimulation_connected=self.is_peer_connected(STIMULATION_PEER),
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

    async def restart_actuation_for_configuration(self, snapshot: dict):
        if not self._admission.is_transition_owner:
            raise RuntimeError("A runtime transition must own output replacement")
        if self._actuation_manager is None:
            if snapshot.get("ACTUATION.enabled", False):
                raise RuntimeError("No managed Actuation process is available")
            return
        await asyncio.to_thread(self._actuation_manager.stop)
        await asyncio.to_thread(self._actuation_manager.start, settings_snapshot=deepcopy(snapshot))

    @property
    def is_initialized(self) -> bool:
        """True once the service can accept composition-time subscriptions."""
        return self._initialized or self._engine is not None

    @property
    def is_engine_ready(self) -> bool:
        """True only after the engine has crossed its startup barrier."""
        thread = self._engine_thread
        return bool(
            self._engine is not None
            and self._running
            and self._accepting_commands
            and thread is not None
            and thread.is_alive()
        )

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
    def applied_revision(self) -> str | None:
        with self._configuration_lock:
            return (
                self._applied_snapshot.get("CONFIG.revision")
                if self._applied_snapshot is not None else None
            )

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
        except BaseException:
            # Startup failures also cross the explicit readiness barrier. Keep
            # the thread exception contained so recovery owns the failed graph.
            logger.exception("NeuroEngine execution failed")
        finally:
            self._accepting_commands = False

    def _engine_shutdown_complete(self) -> bool:
        """Read the optional concrete lifecycle signal without trusting mocks."""
        value = getattr(self._engine, "shutdown_complete", True)
        return value if isinstance(value, bool) else True
