"""
WebSocket endpoint: /api/v1/ws/results

Data chain:
  SignalProcessor
    → BCISession.record_trial()
    → TrialCompleted domain event
    → EventBus (EventType.TrialCompleted, payload=Classification VO)
    → ResultsBroadcaster.on_trial_completed()
    → JSON fan-out to WebSocket clients
"""
from __future__ import annotations

import asyncio
import logging
from typing import Set

from fastapi import WebSocket, WebSocketDisconnect

from omnibci.shared.events import Event
from omnibci.infrastructure.outbound import event_to_wire
from omnibci.presentation.schemas.ws import (
    SessionEndedMessage,
    SessionStartedMessage,
    StimulationAckMessage,
    TrialCompletedMessage,
)

# The unified outbound mapping (event_to_wire) is the field source; the pydantic
# ws schemas remain the canonical contract and validate each message.
_WIRE_MODELS = {
    "TrialCompleted": TrialCompletedMessage,
    "SessionStarted": SessionStartedMessage,
    "SessionEnded": SessionEndedMessage,
    "StimulationAck": StimulationAckMessage,
}

logger = logging.getLogger(__name__)


class ResultsBroadcaster:
    """
    Bridges the synchronous EventBus (NeuroEngine thread) and async
    WebSocket clients (uvicorn asyncio loop).
    """

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        logger.info("WS client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        logger.info("WS client disconnected (%d remaining)", len(self._clients))

    # ------------------------------------------------------------------
    # EventBus callbacks (called from NeuroEngine thread, sync)
    # ------------------------------------------------------------------

    def _broadcast_event(self, event: Event) -> None:
        """Map an Event to its outbound ws message via the unified contract."""
        if not self._clients or self._loop is None:
            return
        wire = event_to_wire(event)
        if wire is None:
            return
        model_cls = _WIRE_MODELS.get(wire.get("event"))
        if model_cls is None:
            logger.warning("No ws model for outbound event %r", wire.get("event"))
            return
        self._schedule(model_cls(**wire).model_dump_json())

    # Named handlers kept as the EventBus subscription seam (see bootstrap).
    def on_trial_completed(self, event: Event) -> None:
        self._broadcast_event(event)

    def on_session_started(self, event: Event) -> None:
        self._broadcast_event(event)

    def on_session_ended(self, event: Event) -> None:
        self._broadcast_event(event)

    def on_stimulation_ack(self, event: Event) -> None:
        self._broadcast_event(event)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule(self, json_str: str) -> None:
        asyncio.run_coroutine_threadsafe(self._broadcast(json_str), self._loop)

    async def _broadcast(self, message: str) -> None:
        dead: Set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.disconnect(ws)


results_broadcaster = ResultsBroadcaster()


async def results_ws_endpoint(websocket: WebSocket) -> None:
    """FastAPI WebSocket handler — mounted at /api/v1/ws/results."""
    await results_broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        results_broadcaster.disconnect(websocket)
