"""
PeerTransport — uniform full-duplex link to one external peer.

Two topologies, one interface:
  - ListenTransport  : neuroengine *listens*; the peer dials in (stimulation
                       displays, or an actuation controller that connects out).
                       Wraps NetworkServer; a single listen port may hold several
                       homogeneous connections (e.g. multiple displays) and
                       ``send`` fans out to all of them.
  - ConnectTransport : neuroengine *dials out* to a peer that hosts its own
                       server (e.g. an actuation controller). Wraps NetworkClient.

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
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity

logger = logging.getLogger(__name__)

InboundHandler = Callable[[Event], bool]
LivenessHandler = Callable[[], None]


class PeerTransport(abc.ABC):
    """Full-duplex framed link to a single external peer."""

    @abc.abstractmethod
    def set_inbound_handler(self, handler: InboundHandler) -> None: ...

    @abc.abstractmethod
    def set_liveness_handler(self, handler: LivenessHandler) -> None:
        """Register a callback fired for keep-alives and accepted Event frames."""
        ...

    @abc.abstractmethod
    async def start(self) -> None:
        """Begin serving/connecting. Returns once the link is running in the
        background (does not block on serve_forever / the receive loop)."""

    @abc.abstractmethod
    async def stop(self) -> None: ...

    @abc.abstractmethod
    async def send(self, event: Event) -> bool:
        """Write to an active socket; True is not a peer/application ack."""

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...


class ListenTransport(PeerTransport):
    """neuroengine listens; peer(s) dial in. Wraps NetworkServer."""

    def __init__(
        self,
        host: str,
        port: int,
        max_clients: int | None = None,
        *,
        idle_timeout: float | None = None,
        write_timeout: float | None = None,
        identity: PeerIdentity | None = None,
    ) -> None:
        timeout_options = {
            name: value
            for name, value in (
                ("idle_timeout", idle_timeout),
                ("write_timeout", write_timeout),
            )
            if value is not None
        }
        if identity is not None:
            timeout_options["identity"] = identity
        self._server = NetworkServer(
            event_bus=None,
            host=host,
            port=port,
            max_clients=max_clients,
            **timeout_options,
        )
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._server.set_inbound_handler(handler)

    def set_liveness_handler(self, handler: LivenessHandler) -> None:
        self._server.set_liveness_handler(handler)

    async def start(self) -> None:
        """Bind the listening socket and return only once it is ready."""
        async with self._lifecycle_lock:
            if self._started:
                return
            # Establish the cleanup obligation before awaiting: NetworkServer.start
            # may allocate a listener and then fail.  If rollback also fails,
            # stop() must remain able to retry instead of treating this transport
            # as never started.
            self._started = True
            try:
                # NetworkServer owns the asyncio.Server.  Its start contract is
                # readiness-based: once this await completes the port is bound and
                # asyncio is already accepting clients in the background.
                await self._server.start()
            except BaseException as start_exc:
                # A bind may fail after allocating part of the server state.  The
                # rollback keeps a failed start retryable and leaves no hidden task.
                try:
                    await self._server.stop()
                except Exception as cleanup_exc:
                    logger.error(
                        "ListenTransport: cleanup after failed start failed: %s",
                        cleanup_exc,
                    )
                    # Keep _started=True so stop() can retry the owned server.
                    raise cleanup_exc from start_exc
                self._started = False
                raise

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            # Only mark stopped after NetworkServer confirms that the listener and
            # accepted connections are closed.  A failed stop can then be retried.
            await self._server.stop()
            self._started = False

    async def send(self, event: Event) -> bool:
        return await self._server.send_event(event)

    @property
    def is_connected(self) -> bool:
        return self._server.is_connected


class ConnectTransport(PeerTransport):
    """neuroengine dials out to a peer that hosts its own server. Wraps NetworkClient."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 0.5,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 10.0,
        idle_timeout: float | None = None,
        write_timeout: float | None = None,
        identity: PeerIdentity | None = None,
        reconnect: bool = True,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        client_options = {
            "connect_timeout": connect_timeout,
            "reconnect_initial": reconnect_initial,
            "reconnect_max": reconnect_max,
        }
        if identity is not None:
            client_options["identity"] = identity
        if not reconnect:
            client_options["reconnect"] = False
        client_options.update({
            name: value
            for name, value in (
                ("idle_timeout", idle_timeout),
                ("write_timeout", write_timeout),
            )
            if value is not None
        })
        self._client = NetworkClient(
            event_bus=None,
            server_ip=host,
            server_port=port,
            **client_options,
        )
        self._run_task: Optional[asyncio.Task] = None
        self._lifecycle_lock = asyncio.Lock()
        self._last_supervisor_error: Optional[BaseException] = None
        self._on_disconnect = on_disconnect
        self._stopping = False

    def set_inbound_handler(self, handler: InboundHandler) -> None:
        self._client.set_inbound_handler(handler)

    def set_liveness_handler(self, handler: LivenessHandler) -> None:
        self._client.set_liveness_handler(handler)

    async def start(self) -> None:
        """Start exactly one reconnecting client supervisor in the background."""
        async with self._lifecycle_lock:
            if self._run_task is not None and not self._run_task.done():
                return
            if self._client_cleanup_pending:
                # A completed supervisor may have retained an unconfirmed writer.
                # Resolve that synchronously before advertising a replacement
                # supervisor; otherwise start() would appear successful while the
                # fatal cleanup error remained only in a background task.
                await self._client.stop()
                if self._client_cleanup_pending:
                    raise RuntimeError(
                        "ConnectTransport cannot restart with pending client cleanup"
                    )
            # A previous supervisor may have ended unexpectedly.  Its done callback
            # has already consumed/logged the exception; replace it with one owner.
            self._last_supervisor_error = None
            self._stopping = False
            self._run_task = asyncio.create_task(
                self._run_client(),
                name=f"peer-connect-{self._client.server_ip}:{self._client.server_port}",
            )
            self._run_task.add_done_callback(self._harvest_supervisor)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopping = True
            task = self._run_task
            if task is None:
                if self._client_cleanup_pending:
                    await self._client.stop()
                return
            if not task.done():
                task.cancel()
            try:
                try:
                    # Do not let cancellation of this *caller* interrupt the unique
                    # supervisor's ``finally: client.stop()`` cleanup.  Ownership is
                    # retained until that task has really reached a terminal state.
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            # The done callback owns reporting the supervisor error;
                            # preserve the caller's cancellation after cleanup.
                            pass
                        raise
                    # The deliberately-cancelled supervisor has finished cleanup.
                except Exception:
                    # _harvest_supervisor owns reporting.  Awaiting here still ensures
                    # the completed task and its client cleanup have been collected.
                    pass
                if self._client_cleanup_pending:
                    # NetworkClient truthfully retains a writer whose wait_closed()
                    # was not confirmed.  Retry here and propagate a persistent
                    # cleanup failure so PeerChannel keeps its ownership flag.
                    await self._client.stop()
            finally:
                if task.done() and self._run_task is task:
                    self._run_task = None

    @property
    def _client_cleanup_pending(self) -> bool:
        return (
            getattr(self._client, "writer", None) is not None
            or bool(getattr(self._client, "_closing_writers", ()))
        )

    async def _run_client(self) -> None:
        """Own both the client supervisor and its eventual socket cleanup."""
        try:
            await self._client.start()
        finally:
            try:
                await self._client.stop()
            finally:
                if self._on_disconnect is not None and not self._stopping:
                    self._on_disconnect()

    def _harvest_supervisor(self, task: asyncio.Task) -> None:
        """Retrieve every terminal result so background failures are never lost."""
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except BaseException as exc:  # task callbacks must never leak exceptions
            self._last_supervisor_error = exc
            logger.error(
                "ConnectTransport: client supervisor stopped unexpectedly: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def send(self, event: Event) -> bool:
        return await self._client.send_event(event)

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected

    @property
    def terminal_error(self) -> Optional[BaseException]:
        """Last error that terminated the reconnect supervisor, if any."""
        return self._last_supervisor_error
