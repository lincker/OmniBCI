"""
NeuroEngineService — Application Service.

Orchestrates NeuroEngine lifecycle and exposes it to the API layer via
command / query objects.  The API layer never touches NeuroEngine directly.
"""
from __future__ import annotations

import asyncio
import logging
from threading import Thread
from typing import Optional

from omnibci.infrastructure.engine.engine import NeuroEngine
from omnibci.infrastructure.engine.builder import NeuroEngineBuilder, NeuroEngineConfig
from omnibci.shared.events import EventType, Event
from omnibci.application.ports.engine_port import EnginePort
from omnibci.application.commands.engine_commands import (
    StartEngineCommand,
    StopEngineCommand,
    StartSignalProcessorCommand,
    StopSignalProcessorCommand,
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

    def __init__(self) -> None:
        self._engine: Optional[NeuroEngine] = None
        self._engine_thread: Optional[Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Build (but do not start) the NeuroEngine via the builder."""
        self._engine = NeuroEngineBuilder().build()
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
        self._running = False
        logger.info("NeuroEngine stopped")

    def handle_start_signal_processor(self, cmd: StartSignalProcessorCommand) -> None:
        self.dispatch(Event(EventType.StartNeuroEngineSignalProcessor))

    def handle_stop_signal_processor(self, cmd: StopSignalProcessorCommand) -> None:
        self.dispatch(Event(EventType.StopNeuroEngineSignalProcessor))

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    def handle_get_status(self, query: GetEngineStatusQuery) -> EngineStatusResult:
        if self._engine is None:
            return EngineStatusResult(running=False, acquisition_alive=False, processor_alive=False)
        return EngineStatusResult(
            running=self._running,
            acquisition_alive=(
                self._engine.acquisitor_process.is_alive()
                if self._engine.acquisitor_process else False
            ),
            processor_alive=(
                self._engine.signal_processor_process.is_alive()
                if self._engine.signal_processor_process else False
            ),
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

    def subscribe(self, event_type, handler) -> None:
        """
        Register a listener on the engine's event bus.

        Exposed so the composition root can wire EventBus → broadcaster /
        read-model listeners without reaching into ``_engine.event_bus``.
        """
        self._require_engine()
        self._engine.event_bus.register_event_listener(event_type, handler)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_engine(self) -> None:
        if self._engine is None:
            raise RuntimeError("NeuroEngineService not initialised")
