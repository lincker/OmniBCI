"""
NeuroEngineService — Application Service.

Orchestrates NeuroEngine lifecycle and exposes it to the API layer via
command / query objects.  The API layer never touches NeuroEngine directly.
"""

import asyncio
import logging
from threading import Thread
from typing import Optional

from omnibci.shared.events import EventType, Event
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

    def __init__(self, engine_factory: EngineFactory) -> None:
        # 引擎的构建方式由组合根（bootstrap）注入 —— application 层不再
        # import NeuroEngineBuilder（P0-1）。每次调用工厂得到一张全新引擎图。
        self._engine_factory = engine_factory
        self._engine: Optional[EngineHandle] = None
        self._engine_thread: Optional[Thread] = None
        self._running = False
        # mp.Process is single-use and NeuroEngine._stop() closes its queues,
        # so after a stop the whole engine graph must be rebuilt before the
        # next start.  Bus subscriptions made via subscribe() are recorded so
        # they can be replayed onto the fresh EventBus after a rebuild.
        self._needs_rebuild = False
        self._subscriptions: list = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Build (but do not start) the engine via the injected factory."""
        self._engine = self._engine_factory()
        logger.info("NeuroEngineService initialised")

    async def shutdown(self) -> None:
        """Stop everything cleanly."""
        if self._running:
            await self.handle_stop_engine(StopEngineCommand())

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def handle_start_engine(self, cmd: StartEngineCommand) -> None:
        if self._running:
            logger.warning("Engine already running — ignoring StartEngineCommand")
            return
        if self._engine is None:
            raise RuntimeError("NeuroEngineService not initialised; call startup() first")

        if self._needs_rebuild:
            # Rebuild the engine graph (fresh workers, queues, bus, gateway),
            # then replay listeners (session read-model + WS broadcaster) onto
            # the new EventBus — they were registered on the discarded one.
            self._engine = self._engine_factory()
            for event_type, handler in self._subscriptions:
                self._engine.event_bus.register_event_listener(event_type, handler)
            self._needs_rebuild = False
            logger.info(
                "NeuroEngine rebuilt (%d listeners replayed)", len(self._subscriptions)
            )

        # NeuroEngine.run() calls asyncio.run() internally, so we spin it in a thread.
        self._engine_thread = Thread(target=self._engine.run, daemon=True, name="neuro-engine")
        self._engine_thread.start()
        self._running = True
        logger.info("NeuroEngine started")

    async def handle_stop_engine(self, cmd: StopEngineCommand) -> None:
        if not self._running or self._engine is None:
            return
        self._engine.should_stop = True
        if self._engine_thread:
            self._engine_thread.join(timeout=5)
            if self._engine_thread.is_alive():
                logger.warning("Engine thread did not stop within 5s")
        self._running = False
        self._needs_rebuild = True
        logger.info("NeuroEngine stopped")

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    def handle_get_status(self, query: GetEngineStatusQuery) -> EngineStatusResult:
        if self._engine is None:
            return EngineStatusResult(running=False, acquisition_alive=False, processor_alive=False)
        return EngineStatusResult(
            running=self._running,
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
        self._require_engine()
        self._engine.event_bus.put_event(event)

    @property
    def is_engine_ready(self) -> bool:
        """True once the engine (and therefore its event bus) exists."""
        return self._engine is not None

    def is_peer_connected(self, peer_name: str) -> bool:
        """True when the named peer's transport has an active connection.

        Returns False if the engine is not running or the peer doesn't exist.
        Safe to call at any time — never raises.
        """
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
        self._require_engine()
        self._subscriptions.append((event_type, handler))
        self._engine.event_bus.register_event_listener(event_type, handler)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_engine(self) -> None:
        if self._engine is None:
            raise RuntimeError("NeuroEngineService not initialised")
