#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-07-24 09:52
"""Async TCP transport for the shared Event wire contract."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import NamedTuple

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.contracts import (
    InvalidEventBody,
    PROTOCOL_VERSION,
    UnsupportedProtocolVersion,
    decode_event_body,
    encode_event,
    require_supported_version,
)
from .protocol import Frame, FrameProtocolError

logger = logging.getLogger(__name__)

DEFAULT_WRITE_TIMEOUT = 1.0
PEER_HANDSHAKE_TIMEOUT = 5.0


class StreamCleanupError(RuntimeError):
    """A socket/listener close was requested but could not be confirmed."""


def _validate_idle_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("idle_timeout must be a positive number or None")
    return float(value)


def _validate_write_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("write_timeout must be a positive number")
    return float(value)


async def _readexactly(reader, size: int, idle_timeout: float | None) -> bytes:
    operation = reader.readexactly(size)
    if idle_timeout is None:
        return await operation
    return await asyncio.wait_for(operation, timeout=idle_timeout)


class ReadEventFrame(NamedTuple):
    """Result of one complete frame read.

    ``event`` is None for a keep-alive. ``valid`` remains for source compatibility
    and is always True: a complete non-empty body that cannot be decoded now
    raises :class:`InvalidEventBody` and requires disconnecting the peer.
    """

    event: Event | None
    valid: bool


async def read_event_frame(reader, *, idle_timeout: float | None = None) -> ReadEventFrame:
    """Read one bounded, version-checked Event frame from ``reader``.

    Header version and body size are validated before reading the body.  The
    optional timeout applies independently to the header and body reads.  A
    Malformed JSON/event envelopes, unsupported versions, and invalid sizes all
    raise protocol errors.  A peer cannot retain a connection slot by streaming
    complete but unusable Event bodies.
    """
    timeout = _validate_idle_timeout(idle_timeout)
    header_bytes = await _readexactly(reader, Frame.Header.HEADER_SIZE, timeout)
    header = Frame.Header.from_bytes(header_bytes)
    require_supported_version(header.version)
    if header.body_size == 0:
        return ReadEventFrame(event=None, valid=True)
    body = await _readexactly(reader, header.body_size, timeout)
    event = decode_event_body(body, header.version)
    return ReadEventFrame(event=event, valid=True)


async def _close_stream_writer(
    writer,
    write_timeout: float = DEFAULT_WRITE_TIMEOUT,
) -> bool:
    """Request writer close and report whether release was confirmed.

    ``ConnectionError``/``OSError`` from ``wait_closed`` are treated as a
    confirmed dead transport after ``close()``.  Timeouts and unknown failures
    return False so the owner can retain the handle and retry instead of
    advertising a clean shutdown.
    """
    if writer is None:
        return True
    close = getattr(writer, "close", None)
    if not callable(close):
        return True
    try:
        close()
        wait_closed = getattr(writer, "wait_closed", None)
        if callable(wait_closed):
            await asyncio.wait_for(wait_closed(), timeout=write_timeout)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "Timed out after %.3fs while closing peer writer", write_timeout
        )
        return False
    except (ConnectionError, OSError) as exc:
        # The close request was issued and the underlying transport already
        # reports itself dead.  Some event loops surface this from wait_closed().
        logger.debug("Peer writer closed with transport error: %s", exc)
        return True
    except Exception as exc:  # pragma: no cover - platform-specific unknowns
        logger.warning("Could not confirm peer writer close: %s", exc)
        return False
    return True


class NetworkServer:
    def __init__(
        self,
        event_bus=None,
        host=None,
        port=None,
        max_clients: int | None = None,
        idle_timeout: float | None = None,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        identity: PeerIdentity | None = None,
    ):
        if max_clients is not None and max_clients < 1:
            raise ValueError("max_clients must be >= 1 or None")
        self.event_bus = event_bus
        self.host = host
        self.port = port
        self.max_clients = max_clients
        self.idle_timeout = _validate_idle_timeout(idle_timeout)
        self.write_timeout = _validate_write_timeout(write_timeout)
        self.identity = identity
        self.status = "INIT"
        self.server = None
        self._clients: set = set()
        self._pending_clients: set = set()
        self._closing_writers: set = set()
        self._client_tasks: set[asyncio.Task] = set()
        self._inbound_handler = None
        self._liveness_handler = None
        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._stopping = False

    def set_inbound_handler(self, handler) -> None:
        self._inbound_handler = handler

    def set_liveness_handler(self, handler) -> None:
        self._liveness_handler = handler

    def _dispatch_inbound(self, event) -> bool:
        """Dispatch one Event and report whether the application accepted it.

        PeerChannel handlers return False for events outside their route.  The
        legacy EventBus injection path has no rejection result, so a successful
        ``put_event`` is considered accepted.
        """
        if self._inbound_handler is not None:
            return self._inbound_handler(event) is True
        if self.event_bus is not None:
            self.event_bus.put_event(event)
            return True
        return False

    def _notify_liveness(self) -> None:
        if self._liveness_handler is not None:
            self._liveness_handler()

    @property
    def is_connected(self) -> bool:
        """Whether at least one accepted client is currently owned by the server."""
        return bool(self._clients)

    async def start(self) -> None:
        """Bind and begin accepting clients; return only after bind succeeds."""
        async with self._lifecycle_lock:
            if self.server is not None and self.server.is_serving():
                self.status = "RUNNING"
                return
            if self._clients or self._pending_clients or self._closing_writers or self._client_tasks:
                self.status = "STOP_FAILED"
                raise StreamCleanupError(
                    "NetworkServer cannot restart with residual client resources"
                )
            if self.server is not None:
                stale_server = self.server
                stale_server.close()
                try:
                    await asyncio.wait_for(
                        stale_server.wait_closed(),
                        timeout=self.write_timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.status = "STOP_FAILED"
                    raise StreamCleanupError(
                        "previous NetworkServer listener did not close"
                    ) from exc
                self.server = None
            self._stopping = False
            self.status = "STARTING"
            try:
                server = await asyncio.start_server(
                    self.handle_client,
                    host=self.host,
                    port=self.port,
                )
            except asyncio.CancelledError:
                self.status = "INIT"
                raise
            except Exception:
                self.status = "FAILED"
                raise
            self.server = server
            self.status = "RUNNING"

    async def handle_client(self, reader, writer) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        addr = writer.get_extra_info("peername")
        try:
            if self._stopping:
                return
            owned_writers = self._clients | self._pending_clients | self._closing_writers
            if self.max_clients is not None and len(owned_writers) >= self.max_clients:
                logger.warning(
                    "Peer connection rejected from %s: client limit (%d) reached",
                    addr,
                    self.max_clients,
                )
                return

            self._pending_clients.add(writer)
            if self.identity is not None:
                frame = await asyncio.wait_for(
                    read_event_frame(reader), timeout=PEER_HANDSHAKE_TIMEOUT,
                )
                if (frame.event is None or frame.event.type != EventType.PeerHello
                        or not self.identity.matches(frame.event.data)):
                    raise InvalidEventBody("Peer identity does not match the current runtime")
                writer.write(encode_event(Event(EventType.PeerAccepted, self.identity.to_dict())))
                await asyncio.wait_for(writer.drain(), timeout=self.write_timeout)
            if self._stopping:
                return
            self._pending_clients.discard(writer)
            self._clients.add(writer)
            logger.info("Peer client connected: %s (%d total)", addr, len(self._clients))
            while True:
                frame = await read_event_frame(
                    reader,
                    idle_timeout=self.idle_timeout,
                )
                # Keep-alives prove transport liveness directly.  Event frames
                # prove application liveness only after the route owner accepts
                # them; a valid-but-unauthorised Event cannot feed a watchdog.
                if frame.event is None:
                    self._notify_liveness()
                elif self._dispatch_inbound(frame.event):
                    self._notify_liveness()
        except asyncio.IncompleteReadError:
            logger.info("Peer client disconnected: %s", addr)
        except asyncio.TimeoutError:
            logger.warning("Peer client timed out: %s", addr)
        except (
            UnsupportedProtocolVersion,
            InvalidEventBody,
            FrameProtocolError,
        ) as exc:
            logger.warning("Peer protocol violation from %s: %s", addr, exc)
        except (ConnectionError, OSError) as exc:
            logger.info("Peer client connection lost %s: %s", addr, exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Peer client handler failed: %s", addr)
        finally:
            await self._close_owned_writer(writer)
            if task is not None:
                self._client_tasks.discard(task)

    async def _close_owned_writer(self, writer) -> bool:
        """Close a writer while retaining ownership until close is confirmed."""
        self._clients.discard(writer)
        self._pending_clients.discard(writer)
        self._closing_writers.add(writer)
        closed = await _close_stream_writer(writer, self.write_timeout)
        if closed:
            self._closing_writers.discard(writer)
        return closed

    async def stop(self) -> None:
        """Stop accepting, close every client, and await every handler task."""
        # Serialize with an in-flight broadcast.  Without the shared send lock,
        # stop() could return and a late failing send could add its writer back
        # to _closing_writers, resurrecting ownership behind a STOP status.
        async with self._lifecycle_lock, self._send_lock:
            self._stopping = True
            self.status = "STOPPING"

            server = self.server
            listener_error = None
            if server is not None:
                # Stop accepting immediately.  On Python 3.14 wait_closed() may
                # wait for active client transports too, so confirm listener
                # closure only after those clients have been released below.
                server.close()

            # Let already-accepted callbacks enter handle_client and register
            # themselves before taking the task snapshot.
            await asyncio.sleep(0)
            await asyncio.gather(
                *(
                    self._close_owned_writer(writer)
                    for writer in set(self._clients) | set(self._pending_clients) | set(self._closing_writers)
                ),
                return_exceptions=True,
            )

            current = asyncio.current_task()
            while True:
                tasks = [
                    task for task in self._client_tasks
                    if task is not current and not task.done()
                ]
                if not tasks:
                    break
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(0)

            # A callback racing the first snapshot sees _stopping and exits, but
            # close its writer here as a final ownership sweep.
            await asyncio.gather(
                *(
                    self._close_owned_writer(writer)
                    for writer in set(self._clients) | set(self._pending_clients) | set(self._closing_writers)
                ),
                return_exceptions=True,
            )
            self._clients.clear()
            self._client_tasks = {
                task for task in self._client_tasks if not task.done()
            }
            if server is not None:
                try:
                    await asyncio.wait_for(
                        server.wait_closed(),
                        timeout=self.write_timeout,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    listener_error = exc
                    logger.warning("NetworkServer: listener close failed: %s", exc)
                else:
                    self.server = None
            if listener_error is not None or self._closing_writers:
                self.status = "STOP_FAILED"
                details = []
                if listener_error is not None:
                    details.append("listener close was not confirmed")
                if self._closing_writers:
                    details.append(
                        f"{len(self._closing_writers)} writer close(s) were not confirmed"
                    )
                raise StreamCleanupError(
                    "NetworkServer cleanup incomplete: " + "; ".join(details)
                ) from listener_error
            self.status = "STOP"

    async def send_event(self, event: Event) -> bool:
        """Write one Event to at least one active socket.

        True confirms only that a framed write/drain succeeded for one currently
        connected peer; it is not an application-level acknowledgement.
        """
        return await self._send_frame(encode_event(event))

    async def send(self, message: str) -> bool:
        """Legacy write API with the same socket-delivery-only bool contract."""
        return await self._send_frame(
            Frame(message, version=PROTOCOL_VERSION).to_bytes()
        )

    async def _send_frame(self, frame: bytes) -> bool:
        """Broadcast one frame concurrently; isolate clients and frame ordering."""
        async with self._send_lock:
            if self._stopping:
                logger.debug(
                    "NetworkServer.send: server is stopping/stopped; dropping message"
                )
                return False
            clients = list(self._clients)
            if not clients:
                logger.warning(
                    "NetworkServer.send: no peer client connected; dropping message"
                )
                return False

            logger.debug(
                "Send %d-byte frame to %d peer client(s)", len(frame), len(clients)
            )

            async def send_one(writer):
                try:
                    writer.write(frame)
                    await asyncio.wait_for(
                        writer.drain(),
                        timeout=self.write_timeout,
                    )
                    return writer, True
                except Exception as exc:  # pragma: no cover - platform dependent
                    logger.warning("NetworkServer.send: dropping dead client: %s", exc)
                    return writer, False

            results = await asyncio.gather(*(send_one(writer) for writer in clients))
            dead = [writer for writer, ok in results if not ok]
            for writer in dead:
                self._clients.discard(writer)
            await asyncio.gather(
                *(
                    self._close_owned_writer(writer)
                    for writer in dead
                ),
                return_exceptions=True,
            )
            return any(ok for _, ok in results)


class NetworkClient:
    """Dial-out peer link with supervised reconnection."""

    def __init__(
        self,
        event_bus: EventBus = None,
        server_ip="127.0.0.1",
        server_port=8888,
        connect_timeout: float = 0.5,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 10.0,
        idle_timeout: float | None = None,
        write_timeout: float = DEFAULT_WRITE_TIMEOUT,
        identity: PeerIdentity | None = None,
        reconnect: bool = True,
    ):
        self.event_bus = event_bus
        self.server_ip = server_ip
        self.server_port = server_port
        self.reader = None
        self.writer = None
        self._closing_writers: set = set()
        self.status = "INIT"
        self._inbound_handler = None
        self._liveness_handler = None
        self._connect_timeout = connect_timeout
        self._reconnect_initial = reconnect_initial
        self._reconnect_max = reconnect_max
        self._idle_timeout = _validate_idle_timeout(idle_timeout)
        self._write_timeout = _validate_write_timeout(write_timeout)
        self.identity = identity
        self._reconnect = reconnect
        self._should_run = False
        self._supervisor_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    def set_inbound_handler(self, handler) -> None:
        self._inbound_handler = handler

    def set_liveness_handler(self, handler) -> None:
        self._liveness_handler = handler

    def _dispatch_inbound(self, event) -> bool:
        """Dispatch one Event and report whether the application accepted it."""
        if self._inbound_handler is not None:
            return self._inbound_handler(event) is True
        if self.event_bus is not None:
            self.event_bus.put_event(event)
            return True
        return False

    def _notify_liveness(self) -> None:
        if self._liveness_handler is not None:
            self._liveness_handler()

    @property
    def is_connected(self) -> bool:
        """Whether the supervised connection is currently usable for writes."""
        return self.status == "CONNECTED" and self.writer is not None

    async def start(self) -> None:
        """Run connect/receive/reconnect supervision until stopped or cancelled."""
        current = asyncio.current_task()
        async with self._lifecycle_lock:
            existing = self._supervisor_task
            if existing is not None and existing is not current and not existing.done():
                logger.debug("NetworkClient.start: supervisor already running")
                return
            self._supervisor_task = current
            self._should_run = True

        delay = self._reconnect_initial
        try:
            while self._should_run:
                if not await self._connect():
                    if not self._should_run or not self._reconnect:
                        break
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._reconnect_max)
                    continue

                connected_at = time.monotonic()
                try:
                    while self._should_run:
                        await self.receive()
                except (
                    asyncio.IncompleteReadError,
                    asyncio.TimeoutError,
                    UnsupportedProtocolVersion,
                    InvalidEventBody,
                    FrameProtocolError,
                    ConnectionError,
                    OSError,
                ) as exc:
                    if not self._should_run:
                        break
                    logger.info("NetworkClient: link lost (%s)", exc)
                    self.status = "DISCONNECTED"
                    if not await self._cleanup_writers():
                        self.status = "STOP_FAILED"
                        raise StreamCleanupError(
                            "NetworkClient could not close the lost connection"
                        )
                    if not self._reconnect:
                        break
                    if time.monotonic() - connected_at >= self._reconnect_initial:
                        delay = self._reconnect_initial
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._reconnect_max)
        finally:
            self._should_run = False
            cleanup_complete = False
            try:
                # The supervisor can be cancelled while another task is
                # draining an outbound frame.  Use the same send boundary as
                # explicit stop() so its finally block cannot close that writer
                # underneath the in-flight send.
                async with self._send_lock:
                    cleanup_complete = await self._cleanup_writers()
            finally:
                if not cleanup_complete:
                    self.status = "STOP_FAILED"
                elif self.status not in ("STOP", "STOPPING"):
                    self.status = "DISCONNECTED"
                async with self._lifecycle_lock:
                    if self._supervisor_task is current:
                        self._supervisor_task = None
                logger.info("NetworkClient: supervisor loop exited")
            if not cleanup_complete:
                raise StreamCleanupError(
                    "NetworkClient supervisor exited with unclosed writer(s)"
                )

    async def _connect(self) -> bool:
        """Perform one connection attempt without leaking a stop-raced socket."""
        if self.writer is not None or self._closing_writers:
            if not await self._cleanup_writers():
                self.status = "STOP_FAILED"
                raise StreamCleanupError(
                    "NetworkClient cannot reconnect before the old writer closes"
                )
        self.status = "CONNECTING" if self._should_run else self.status
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.server_ip, self.server_port),
                timeout=self._connect_timeout,
            )
        except asyncio.TimeoutError:
            logger.info("Connection to %s:%s timed out", self.server_ip, self.server_port)
        except ConnectionRefusedError:
            logger.info("Connection to %s:%s refused", self.server_ip, self.server_port)
        except OSError as exc:
            logger.info("Connection to %s:%s failed: %s", self.server_ip, self.server_port, exc)
        else:
            accepted = False
            try:
                async with self._connection_lock:
                    if self._should_run and self.writer is None:
                        self.reader = reader
                        self.writer = writer
                        self.status = "HANDSHAKING" if self.identity is not None else "CONNECTED"
                        accepted = True
                if accepted and self.identity is not None:
                    writer.write(encode_event(Event(EventType.PeerHello, self.identity.to_dict())))
                    await asyncio.wait_for(writer.drain(), timeout=self._write_timeout)
                    frame = await asyncio.wait_for(
                        read_event_frame(reader), timeout=PEER_HANDSHAKE_TIMEOUT,
                    )
                    if (frame.event is None or frame.event.type != EventType.PeerAccepted
                            or not self.identity.matches(frame.event.data)):
                        raise InvalidEventBody("Peer identity was not accepted by the engine")
                    async with self._connection_lock:
                        accepted = self._should_run and self.writer is writer
                        if accepted:
                            self.status = "CONNECTED"
            except BaseException:
                if not await self._close_tracked_writer(writer):
                    self.status = "STOP_FAILED"
                raise

            if not accepted:
                if not await self._close_tracked_writer(writer):
                    self.status = "STOP_FAILED"
                    raise StreamCleanupError(
                        "stop-raced NetworkClient socket did not close"
                    )
                return False
            logger.info("Connected to server %s:%s", self.server_ip, self.server_port)
            return True

        if self.status != "STOP":
            self.status = "DISCONNECTED"
        return False

    async def send_event(self, event: Event) -> bool:
        """Write one Event to the active socket, without implying peer handling."""
        return await self._send_frame(encode_event(event))

    async def send(self, message: str) -> bool:
        """Legacy write API with the same socket-delivery-only bool contract."""
        return await self._send_frame(
            Frame(message, version=PROTOCOL_VERSION).to_bytes()
        )

    async def _send_frame(self, frame: bytes) -> bool:
        """Send one frame, returning False for disconnects instead of raising."""
        async with self._send_lock:
            if self.status in ("STOPPING", "STOP", "STOP_FAILED"):
                logger.debug(
                    "NetworkClient.send: client is not writable in state %s",
                    self.status,
                )
                return False
            writer = self.writer
            if writer is None or (self.identity is not None and not self.is_connected):
                logger.warning(
                    "NetworkClient.send: not connected; dropping %d-byte frame",
                    len(frame),
                )
                return False
            try:
                writer.write(frame)
                await asyncio.wait_for(
                    writer.drain(),
                    timeout=self._write_timeout,
                )
                return True
            except Exception as exc:
                logger.warning("NetworkClient.send failed; disconnecting: %s", exc)
                if self.status != "STOP":
                    self.status = "DISCONNECTED"
                if not await self._close_writer(expected_writer=writer):
                    self.status = "STOP_FAILED"
                return False

    async def receive(self) -> None:
        """Read and dispatch one complete, valid frame."""
        reader = self.reader
        if reader is None:
            raise ConnectionError("NetworkClient.receive called without a connection")
        frame = await read_event_frame(reader, idle_timeout=self._idle_timeout)
        if frame.event is None:
            self._notify_liveness()
        elif self._dispatch_inbound(frame.event):
            self._notify_liveness()

    async def stop(self) -> None:
        """Deliberate, idempotent shutdown with no reconnect after this."""
        self._should_run = False
        self.status = "STOPPING"
        async with self._lifecycle_lock:
            task = self._supervisor_task
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if current is not None and current.cancelling():
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    raise
                # Expected result of deliberately cancelling the supervisor.
            except Exception:
                # Cleanup is verified independently below; the supervisor owner
                # reports its own terminal error.
                pass

        # The supervisor's finally block uses this same lock.  Acquire it only
        # after awaiting that task: holding it while waiting would deadlock the
        # supervisor before it could complete its cleanup.
        async with self._send_lock:
            if not await self._cleanup_writers():
                self.status = "STOP_FAILED"
                raise StreamCleanupError(
                    "NetworkClient cleanup incomplete: writer close was not confirmed"
                )
            self.status = "STOP"

    async def _close_tracked_writer(self, writer) -> bool:
        if writer is None:
            return True
        self._closing_writers.add(writer)
        closed = await _close_stream_writer(writer, self._write_timeout)
        if closed:
            self._closing_writers.discard(writer)
        return closed

    async def _close_writer(self, expected_writer=None) -> bool:
        async with self._connection_lock:
            writer = self.writer
            if expected_writer is not None and writer is not expected_writer:
                return True
            closed = await self._close_tracked_writer(writer)
            if closed and self.writer is writer:
                self.writer = None
                self.reader = None
            return closed

    async def _cleanup_writers(self) -> bool:
        """Retry all owned writers and retain every unconfirmed handle."""
        active = self.writer
        await self._close_writer()
        # _close_writer already made one bounded attempt for the active writer.
        # Retry only other orphaned writers here; the active one remains owned for
        # a later lifecycle call if its close could not be confirmed.
        pending = [
            writer for writer in self._closing_writers
            if writer is not active
        ]
        if pending:
            await asyncio.gather(
                *(self._close_tracked_writer(writer) for writer in pending),
                return_exceptions=True,
            )
        if (
            self.writer is not None
            and self.writer not in self._closing_writers
        ):
            # A retry from the pending set confirmed the active writer closed.
            self.writer = None
            self.reader = None
        return not self._closing_writers and self.writer is None
