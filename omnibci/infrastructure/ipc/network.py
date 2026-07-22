#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-07-24 09:52
import logging
import multiprocessing as mp
import asyncio
import time
from omnibci.shared.events import Event
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.contracts import (
    PROTOCOL_VERSION,
    body_to_event,
    event_to_body,
)
from .protocol import Frame

logger = logging.getLogger(__name__)


def _decode_event(raw: bytes) -> "Event | None":
    """Back-compat shim — decode a body at the current protocol version.

    Prefer :func:`omnibci.infrastructure.ipc.contracts.body_to_event`, which also
    validates the frame's protocol version (passed from the header).
    """
    return body_to_event(raw, PROTOCOL_VERSION)


class NetworkServer:
    def __init__(self, event_bus=None, host=None, port=None,
                 max_clients: int | None = None):
        if max_clients is not None and max_clients < 1:
            raise ValueError("max_clients must be >= 1 or None")
        self.event_bus = event_bus
        self.host = host
        self.port = port
        # Stimulation intentionally supports fan-out to multiple displays, while
        # actuation must normally have a single command owner.  Keeping the limit
        # on the transport makes that topology explicit without forking the
        # framing/server implementation.
        self.max_clients = max_clients
        self.status = 'INIT'
        self.server = None
        # Multiple peers may connect to one listen port (e.g. several stimulation
        # displays); a single writer slot would let a reconnect clobber the
        # previous one.
        self._clients: set = set()
        # Inbound dispatch hook. When set (by a PeerChannel), each decoded inbound
        # Event is handed here instead of straight to the EventBus, so the channel
        # can apply its route white-list before anything lands on the bus. Falls
        # back to event_bus.put_event when unset (legacy behaviour).
        self._inbound_handler = None
        # Liveness hook — fired on every received frame (incl. keep-alives) so a
        # PeerChannel watchdog can tell a connected-but-silent peer from a live one.
        self._liveness_handler = None

    def set_inbound_handler(self, handler) -> None:
        """Route inbound Events through *handler(event)* instead of the EventBus."""
        self._inbound_handler = handler

    def set_liveness_handler(self, handler) -> None:
        """Called once per received frame (incl. keep-alives) to signal liveness."""
        self._liveness_handler = handler

    def _dispatch_inbound(self, event) -> None:
        if self._inbound_handler is not None:
            self._inbound_handler(event)
        elif self.event_bus is not None:
            self.event_bus.put_event(event)

    def _notify_liveness(self) -> None:
        if self._liveness_handler is not None:
            self._liveness_handler()

    async def start(self):
        await self._start_server()

    async def _start_server(self):
        self.server = await asyncio.start_server(self.handle_client, host=self.host, port=self.port)
        await self.server.serve_forever()

    async def handle_client(self, reader, writer):
        addr = writer.get_extra_info('peername')
        if self.max_clients is not None and len(self._clients) >= self.max_clients:
            logger.warning(
                "Peer connection rejected from %s: client limit (%d) reached",
                addr,
                self.max_clients,
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # pragma: no cover - defensive close path
                pass
            return
        self._clients.add(writer)
        logger.info("Peer client connected: %s (%d total)", addr, len(self._clients))
        try:
            while True:
                header_bytes = await reader.readexactly(Frame.Header.HEADER_SIZE)
                header = Frame.Header.from_bytes(header_bytes)
                self._notify_liveness()  # any frame (incl. keep-alive) proves liveness
                if header.body_size == 0:
                    continue  # empty keep-alive frame; nothing to dispatch
                body = await reader.readexactly(header.body_size)
                event = body_to_event(body, header.version)  # version-checked
                if event is not None:
                    self._dispatch_inbound(event)
        except asyncio.IncompleteReadError:
            logger.info("Peer client disconnected: %s", addr)
        finally:
            self._clients.discard(writer)

    async def stop(self):
        if self.server:
            self.server.close()
        self.status = 'STOP'

    async def send_event(self, event: Event):
        await self.send(event_to_body(event))

    async def send(self, message):
        if not self._clients:
            logger.warning("NetworkServer.send: no peer client connected; dropping message")
            return
        frame = Frame(message, version=PROTOCOL_VERSION).to_bytes()
        logger.debug("Send to %d peer client(s): %s", len(self._clients), message)
        dead = set()
        for writer in list(self._clients):
            try:
                writer.write(frame)
                await writer.drain()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("NetworkServer.send: dropping dead client: %s", exc)
                dead.add(writer)
        for writer in dead:
            self._clients.discard(writer)


class NetworkClient:
    """
    Dial-out peer link with supervised reconnection.

    ``start()`` runs a supervisor loop: connect (with exponential backoff,
    ``reconnect_initial`` → ×2 → capped at ``reconnect_max``) then receive until
    the link drops, then loop back and reconnect — until ``stop()`` is called or
    the owning task is cancelled. Events arriving while disconnected are dropped
    by the *server* side (engine logs "no client connected"); missed commands are
    NOT replayed after reconnect — real-time semantics by design.
    """

    def __init__(self, event_bus: EventBus = None, server_ip='127.0.0.1', server_port=8888,
                 connect_timeout: float = 0.5,
                 reconnect_initial: float = 1.0,
                 reconnect_max: float = 10.0):
        self.event_bus = event_bus
        self.server_ip = server_ip
        self.server_port = server_port
        self.reader = None
        self.writer = None
        self.status = 'INIT'
        self._inbound_handler = None
        self._liveness_handler = None
        self._connect_timeout = connect_timeout
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._should_run = False

    def set_inbound_handler(self, handler) -> None:
        """Route inbound Events through *handler(event)* instead of the EventBus."""
        self._inbound_handler = handler

    def set_liveness_handler(self, handler) -> None:
        """Called once per received frame (incl. keep-alives) to signal liveness."""
        self._liveness_handler = handler

    def _dispatch_inbound(self, event) -> None:
        if self._inbound_handler is not None:
            self._inbound_handler(event)
        elif self.event_bus is not None:
            self.event_bus.put_event(event)

    def _notify_liveness(self) -> None:
        if self._liveness_handler is not None:
            self._liveness_handler()

    async def start(self):
        """
        Supervisor loop: connect (with backoff) → receive until the link drops →
        reconnect. Exits when stop() clears _should_run or the task is cancelled
        (CancelledError propagates — it is deliberately not caught here).
        """
        self._should_run = True
        delay = self._reconnect_initial
        while self._should_run:
            # ---- Phase 1: connect, with exponential backoff -------------
            if not await self._connect():
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_max)
                continue
            connected_at = time.monotonic()

            # ---- Phase 2: receive until the link drops ------------------
            try:
                while self._should_run:
                    await self.receive()
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
                if not self._should_run:
                    break  # stop() closed the socket under us — clean exit
                logger.info("NetworkClient: link lost (%s); reconnecting", exc)
                self.status = 'DISCONNECTED'
                await self._close_writer()
                # A server-side client limit is observed as a successful TCP
                # connect followed by an immediate EOF.  Retrying that path
                # without a delay creates a tight connection/logging loop.
                # Long-lived connections reset the backoff; short-lived ones
                # keep escalating until the peer becomes available.
                if time.monotonic() - connected_at >= self._reconnect_initial:
                    delay = self._reconnect_initial
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_max)
        logger.info("NetworkClient: supervisor loop exited")

    async def _connect(self) -> bool:
        """One connection attempt. All failures are absorbed; returns success."""
        try:
            # wait_for instead of `async with asyncio.timeout(...)`: the timeout
            # is converted to TimeoutError *inside* this try, so it can never
            # escape and kill the supervisor task (the old bug). Also drops the
            # Python ≥3.11 requirement.
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.server_ip, self.server_port),
                timeout=self._connect_timeout,
            )
            logger.info("Connected to server %s:%s", self.server_ip, self.server_port)
            self.status = 'CONNECTED'
            return True
        except asyncio.TimeoutError:
            logger.info("Connection to %s:%s timed out", self.server_ip, self.server_port)
        except ConnectionRefusedError:
            logger.info("Connection to %s:%s refused", self.server_ip, self.server_port)
        except OSError as exc:
            logger.info("Connection to %s:%s failed: %s", self.server_ip, self.server_port, exc)
        self.status = 'DISCONNECTED'
        return False

    async def send_event(self, event):
        await self.send(event_to_body(event))

    async def send(self, message):
        # Guard on writer (nulled by _close_writer during disconnect/reconnect)
        # rather than status, so a directly-injected writer (tests) still works.
        if self.writer is None:
            logger.warning("NetworkClient.send: not connected; dropping message: %s", message)
            return
        logger.debug("NetworkClient send: %s", message)
        self.writer.write(Frame(message, version=PROTOCOL_VERSION).to_bytes())
        await self.writer.drain()

    async def receive(self):
        """
        Read and dispatch one frame.

        Raises (IncompleteReadError / ConnectionError / OSError) when the link
        drops — the supervisor in start() owns the state transition and
        reconnect. No silent status flip here (the old busy-loop bug).
        """
        header_bytes = await self.reader.readexactly(Frame.Header.HEADER_SIZE)
        header = Frame.Header.from_bytes(header_bytes)
        self._notify_liveness()  # any frame (incl. keep-alive) proves liveness
        if header.body_size == 0:
            return  # empty keep-alive frame
        body = await self.reader.readexactly(header.body_size)
        event = body_to_event(body, header.version)  # version-checked
        if event is not None:
            self._dispatch_inbound(event)

    async def stop(self):
        """Deliberate shutdown: no reconnect after this."""
        self._should_run = False
        self.status = 'STOP'
        await self._close_writer()

    async def _close_writer(self):
        if self.writer is None:
            return
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("NetworkClient: error during close: %s", exc)
        finally:
            self.writer = None
            self.reader = None
