#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-07-24 09:52
import logging
import multiprocessing as mp
import asyncio
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
    def __init__(self, event_bus=None, host=None, port=None):
        self.event_bus = event_bus
        self.host = host
        self.port = port
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
        self._clients.add(writer)
        logger.info("Stimulation client connected: %s (%d total)", addr, len(self._clients))
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
            logger.info("Stimulation client disconnected: %s", addr)
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
            logger.warning("NetworkServer.send: no stimulation client connected; dropping message")
            return
        frame = Frame(message, version=PROTOCOL_VERSION).to_bytes()
        logger.debug("Send to %d stimulation client(s): %s", len(self._clients), message)
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
    def __init__(self, event_bus: EventBus = None, server_ip='127.0.0.1', server_port=8888):
        self.event_bus = event_bus
        self.server_ip = server_ip
        self.server_port = server_port
        self.reader = None
        self.writer = None
        self.status = 'INIT'
        self._inbound_handler = None
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
        # while self.status != 'CONNECTED':
        #     # TODO: asyncio.timeout need python 3.11
        #     async with asyncio.timeout(0.5):
        #         print(f"Connecting to server {self.server_ip}:{self.server_port}")
        #         await self._connect()
        #     await asyncio.sleep(3)
        #TODO: refactor reconnection. Now the client must wait for the server to be ready.
        while self.status != 'CONNECTED':
            async with asyncio.timeout(0.5):
                logging.info(f"Connecting to server {self.server_ip}:{self.server_port}")
                await self._connect()
            await asyncio.sleep(3)



        # self.data_source, self.writer = await asyncio.open_connection(self.server_ip, self.server_port)
        # print(f"Connected to server {self.server_ip}:{self.server_port}")
        # self.status = 'CONNECTED'
        while True:
            # async with asyncio.timeout(2):
            # must use real program to test
            await self.receive()
            # await asyncio.sleep(0.1)

    async def _connect(self):
        try:
            self.reader, self.writer = await asyncio.open_connection(self.server_ip, self.server_port)
            logging.info(f"Connected to server {self.server_ip}:{self.server_port}")
            self.status = 'CONNECTED'
        except ConnectionRefusedError:
            logging.info(f"Connection to server {self.server_ip}:{self.server_port} refused.")
            self.status = 'DISCONNECTED'
        except asyncio.TimeoutError:
            logging.info(f"Connection to server {self.server_ip}:{self.server_port} timeout.")
            self.status = 'DISCONNECTED'
        except Exception as e:
            logging.info(f"Connection to server {self.server_ip}:{self.server_port} error: {e}")
            self.status = 'DISCONNECTED'

    async def send_event(self, event):
        await self.send(event_to_body(event))

    async def send(self, message):
        logger.debug("NetworkClient send: %s", message)
        self.writer.write(Frame(message, version=PROTOCOL_VERSION).to_bytes())
        await self.writer.drain()

    async def receive(self):
        try:
            header_bytes = await self.reader.readexactly(Frame.Header.HEADER_SIZE)
            header = Frame.Header.from_bytes(header_bytes)
            self._notify_liveness()  # any frame (incl. keep-alive) proves liveness
            if header.body_size == 0:
                return  # empty keep-alive frame
            body = await self.reader.readexactly(header.body_size)
            event = body_to_event(body, header.version)  # version-checked
            if event is not None:
                self._dispatch_inbound(event)
        except asyncio.IncompleteReadError:
            logger.info("NetworkClient: server closed the connection")
            self.status = 'STOP'

    async def stop(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("NetworkClient.stop: error during close: %s", exc)
        self.status = 'STOP'
