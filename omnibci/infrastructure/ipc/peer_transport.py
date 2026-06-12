"""
PeerTransport — uniform full-duplex link to one external peer.

Two topologies, one interface:
  - ListenTransport  : neuroengine *listens*; the peer dials in (stimulation
                       displays, or an effector controller that connects out).
                       Wraps NetworkServer; a single listen port may hold several
                       homogeneous connections (e.g. multiple displays) and
                       ``send`` fans out to all of them.
  - ConnectTransport : neuroengine *dials out* to a peer that hosts its own
                       server (e.g. an effector box). Wraps NetworkClient.

A transport is pure plumbing: framing + connection lifecycle. It does NOT decide
what goes on the EventBus — inbound Events are handed to the handler installed via
``set_inbound_handler`` (the owning PeerChannel applies its route white-list).
"""

import abc
import asyncio
import logging
from typing import Callable, Optional

from omnibci.shared.events import Event
from omnibci.infrastructure.ipc.network import NetworkServer, NetworkClient

logger = logging.getLogger(__name__)

InboundHandler = Callable[[Event], None]
LivenessHandler = Callable[[], None]


class PeerTransport(abc.ABC):
    """Full-duplex framed link to a single external peer."""

    @abc.abstractmethod
    def set_inbound_handler(self, handler: InboundHandler) -> None: ...

    def set_liveness_handler(self, handler: LivenessHandler) -> None:
        """Register a callback fired on every received frame (incl. keep-alives).

        Default no-op so transports that don't support liveness signalling stay
        valid; ListenTransport/ConnectTransport override it.
        """

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin serving/connecting. Returns once the link is running in the
        background (does not block on serve_forever / the receive loop)."""

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send(self, event: Event) -> None: ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...


class ListenTransport(PeerTransport):
    """neuroengine listens; peer(s) dial in. Wraps NetworkServer."""

    def __init__(self, host: str, port: int) -> None:
        self._server = NetworkServer(event_bus=None, host=host, port=port)
        self._serve_task: Optional[asyncio.Task] = None

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._server.set_inbound_handler(handler)

    def set_liveness_handler(self, handler: LivenessHandler) -> None:
        self._server.set_liveness_handler(handler)

    async def start(self) -> None:
        # serve_forever() blocks, so run it as a background task and keep a strong
        # reference (a weakly-referenced task may be GC'd mid-run).
        self._serve_task = asyncio.create_task(self._server.start())

    async def stop(self) -> None:
        await self._server.stop()
        if self._serve_task is not None and not self._serve_task.done():
            self._serve_task.cancel()
            try:
                await self._serve_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ListenTransport: error awaiting serve task: %s", exc)
        self._serve_task = None

    async def send(self, event: Event) -> None:
        await self._server.send_event(event)

    @property
    def is_connected(self) -> bool:
        return len(self._server._clients) > 0


class ConnectTransport(PeerTransport):
    """neuroengine dials out to a peer that hosts its own server. Wraps NetworkClient."""

    def __init__(self, host: str, port: int) -> None:
        self._client = NetworkClient(event_bus=None, server_ip=host, server_port=port)
        self._run_task: Optional[asyncio.Task] = None

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._client.set_inbound_handler(handler)

    def set_liveness_handler(self, handler: LivenessHandler) -> None:
        self._client.set_liveness_handler(handler)

    async def start(self) -> None:
        # NetworkClient.start() loops: (re)connect then receive forever.
        self._run_task = asyncio.create_task(self._client.start())

    async def stop(self) -> None:
        await self._client.stop()
        if self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ConnectTransport: error awaiting run task: %s", exc)
        self._run_task = None

    async def send(self, event: Event) -> None:
        await self._client.send_event(event)

    @property
    def is_connected(self) -> bool:
        return self._client.status == 'CONNECTED'
