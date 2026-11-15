"""
PeerChannel — one peer's link + routing rules + connection safety.

Outbound: subscribes the channel's ``routes.outbound`` types on the EventBus;
each such event is forwarded to the peer transport.
Inbound:  the transport hands decoded Events to ``_on_inbound``, which injects
them onto the bus only if their type is in ``routes.inbound`` (white-list).

Safety (Phase 4, only when ``failsafe`` and/or ``watchdog_timeout_s`` are set):
a background monitor watches the link and trips the channel's ``failsafe`` when
  - the link drops (connected → disconnected edge), or
  - the peer is connected but silent past ``watchdog_timeout_s`` (no frame —
    real or keep-alive — within the window).

``failsafe()`` returns Events that are published onto the *bus* (internal alarm /
halt), NOT sent to the peer: a dead link can't carry a safe-stop command, so
device-side dead-man behaviour remains the controller's responsibility.
"""

import asyncio
import logging
import time
from typing import NamedTuple, Optional
from uuid import uuid4

from omnibci.application.events import CommandRequest
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.ipc.network import PeerConnectionChanged
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig

logger = logging.getLogger(__name__)

# How often the monitor re-evaluates link health, in seconds.
_MONITOR_POLL_S = 0.1
_OUTBOUND_CAPACITY = 64
_SEND_TIMEOUT_S = 1.0
_SENDER_STOP_TIMEOUT_S = 2.0


class _Outbound(NamedTuple):
    event: Event
    connection_token: object
    completion: asyncio.Future | None = None


class PeerChannel:
    def __init__(self, config: PeerConfig, bus: EventBus, *,
                 outbound_capacity: int = _OUTBOUND_CAPACITY) -> None:
        if type(outbound_capacity) is not int or outbound_capacity < 1:
            raise ValueError("outbound_capacity must be a positive integer")
        self._config = config
        self._bus = bus
        self._transport = config.transport
        # Safety/monitor state.
        self._last_seen: Optional[float] = None
        self._was_connected: bool = False
        self._stale_tripped: bool = False
        self._monitor_fault: Optional[BaseException] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._registered_outbound = set()
        self._transport_started: bool = False
        self._started: bool = False
        self._dirty: bool = False
        self._lifecycle_lock = asyncio.Lock()
        self._channel_id = uuid4().hex
        self._outbound = asyncio.Queue(maxsize=outbound_capacity)
        self._sender_task: asyncio.Task | None = None
        self._in_flight: _Outbound | None = None
        self._delivery_fault: str | None = None

    @property
    def channel_id(self) -> str:
        return self._channel_id

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        """True only while the channel is started and the peer is healthy."""
        return (
            self._started
            and not self._dirty
            and not self._stale_tripped
            and self._monitor_fault is None
            and self._delivery_fault is None
            and self._transport.is_connected
        )

    @property
    def is_ready(self) -> bool:
        """The transport is prepared, even when no external node is connected."""
        return (self._started and not self._dirty and self._monitor_fault is None
                and self._delivery_fault is None)

    @property
    def is_dirty(self) -> bool:
        """Whether a failed lifecycle operation still needs explicit cleanup."""
        return self._dirty

    @property
    def monitor_fault(self) -> Optional[BaseException]:
        """The exception that disabled watchdog monitoring, if any."""
        return self._monitor_fault

    @property
    def has_lifecycle_resources(self) -> bool:
        """Whether stop() still has work (or a prior cleanup failure to retry)."""
        return (
            self._dirty
            or self._started
            or self._transport_started
            or bool(self._registered_outbound)
            or self._monitor_task is not None
            or self._sender_task is not None
        )

    @property
    def _monitored(self) -> bool:
        return (self._config.failsafe is not None
                or self._config.watchdog_timeout_s is not None)

    async def start(self) -> None:
        """
        Wire routing + safety and bring the link up.

        The await completes only after the transport's own readiness contract is
        satisfied (a listening transport has bound its port; a connecting
        transport has installed its sole background supervisor).  Any failure is
        rolled back before it is re-raised, so callers never inherit half-wired
        EventBus listeners or a partially started transport.
        """
        async with self._lifecycle_lock:
            if self.is_ready:
                return

            # A previous stop may have failed after completing only part of its
            # cleanup.  Finish that cleanup before attempting a fresh start.
            if self.has_lifecycle_resources:
                await self._cleanup_shielded()

            self._reset_safety_state()
            self._channel_id = uuid4().hex
            self._delivery_fault = None
            try:
                self._transport.set_inbound_handler(self._on_inbound)
                self._transport.set_liveness_handler(self._on_liveness)
                for event_type in self._config.routes.outbound:
                    self._bus.register_event_listener(event_type, self._forward)
                    self._registered_outbound.add(event_type)

                # Mark the cleanup obligation before awaiting: start() may be
                # cancelled or fail after allocating transport resources.
                self._transport_started = True
                await self._transport.start()

                if self._monitored:
                    self._monitor_task = asyncio.create_task(
                        self._monitor(),
                        name=f"peer-watchdog-{self._config.name}",
                    )
                self._started = True
                self._dirty = False
                self._sender_task = asyncio.create_task(
                    self._send_outbound(), name=f"peer-sender-{self.name}",
                )
                self._sender_task.add_done_callback(self._harvest_sender)
            except BaseException as startup_exc:
                try:
                    await self._cleanup_shielded()
                except BaseException as cleanup_exc:
                    logger.warning(
                        "PeerChannel '%s': rollback after failed start failed: %s",
                        self._config.name,
                        cleanup_exc,
                    )
                    if isinstance(startup_exc, asyncio.CancelledError):
                        # Cancellation remains the externally visible outcome;
                        # chaining keeps the cleanup failure observable too.
                        raise startup_exc from cleanup_exc
                    raise BaseExceptionGroup(
                        f"PeerChannel '{self._config.name}' start and rollback failed",
                        [startup_exc, cleanup_exc],
                    )
                raise

            logger.info(
                "PeerChannel '%s' up: out=%s in=%s watchdog=%s",
                self._config.name,
                sorted(int(t) for t in self._config.routes.outbound),
                sorted(int(t) for t in self._config.routes.inbound),
                self._config.watchdog_timeout_s,
            )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._cleanup_shielded()

    async def wait_until_connected(self, timeout_s: float) -> None:
        """Wait until the started channel has a healthy socket connection.

        ``start()`` intentionally means that the transport supervisor exists;
        reconnecting clients may still be between attempts.  Managed child
        processes use this stronger barrier before reporting READY to their
        parent.  Manually launched peers keep the original reconnect-forever
        behaviour by simply not calling this method.
        """
        if timeout_s <= 0:
            raise ValueError("connection timeout must be greater than zero")
        if not self._started or self._dirty:
            raise RuntimeError(
                f"PeerChannel '{self._config.name}' is not cleanly started"
            )

        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if self.is_connected:
                return
            if self._monitor_fault is not None:
                raise RuntimeError(
                    f"PeerChannel '{self._config.name}' watchdog failed"
                ) from self._monitor_fault
            transport_error = getattr(self._transport, "terminal_error", None)
            if transport_error is not None:
                raise RuntimeError(
                    f"PeerChannel '{self._config.name}' transport stopped"
                ) from transport_error
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"PeerChannel '{self._config.name}' did not connect within "
                    f"{timeout_s:g}s"
                )
            await asyncio.sleep(min(_MONITOR_POLL_S, remaining))

    @property
    def _has_lifecycle_resources(self) -> bool:
        """Backward-compatible private alias used by older tests/callers."""
        return self.has_lifecycle_resources

    async def _cleanup_shielded(self) -> None:
        """Finish cleanup even if the task awaiting stop() is cancelled."""
        cleanup_task = asyncio.create_task(
            self._stop_locked(),
            name=f"peer-cleanup-{self._config.name}",
        )
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError as cancellation:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                try:
                    await cleanup_task
                except BaseException as cleanup_exc:
                    raise cancellation from cleanup_exc
                raise

            # The cleanup task itself was cancelled rather than its caller.
            # Awaiting it retrieves and propagates that cancellation normally.
            await cleanup_task

    async def _stop_locked(self) -> None:
        """Best-effort cleanup; caller must hold ``_lifecycle_lock``."""
        errors: list[BaseException] = []
        was_started = self._started
        self._started = False

        # Cancel the monitor first so a deliberate shutdown cannot be observed as
        # a disconnect and converted into a failsafe event.
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is not None:
            if not monitor.done():
                monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
            except BaseException as exc:  # harvest an already-failed monitor task
                errors.append(exc)
                logger.warning(
                    "PeerChannel '%s': watchdog cleanup failed: %s",
                    self._config.name,
                    exc,
                )

        for event_type in list(self._registered_outbound):
            try:
                self._bus.unregister_event_listener(event_type, self._forward)
            except BaseException as exc:  # continue so transport is still stopped
                errors.append(exc)
                logger.warning(
                    "PeerChannel '%s': failed to unregister %s: %s",
                    self._config.name,
                    event_type,
                    exc,
                )
            else:
                self._registered_outbound.discard(event_type)

        sender = self._sender_task
        if sender is not None:
            if not sender.done():
                sender.cancel()
                await asyncio.wait({sender}, timeout=_SENDER_STOP_TIMEOUT_S)
            if not sender.done():
                errors.append(TimeoutError(f"PeerChannel '{self.name}' sender did not stop"))
            else:
                self._sender_task = None
                try:
                    sender.result()
                except asyncio.CancelledError:
                    pass
                except BaseException as exc:
                    errors.append(exc)
        self._discard_outbound()

        if self._transport_started and self._sender_task is None:
            try:
                await self._transport.stop()
            except BaseException as exc:
                errors.append(exc)
                logger.warning(
                    "PeerChannel '%s': transport cleanup failed: %s",
                    self._config.name,
                    exc,
                )
            else:
                self._transport_started = False

        # Any cleanup error keeps the channel dirty, even if the failing action
        # appears to have released its resource.  A later stop() is the explicit
        # acknowledgement/retry that clears this conservative state.
        self._dirty = (
            bool(errors)
            or bool(self._registered_outbound)
            or self._transport_started
            or self._sender_task is not None
        )
        if self._dirty:
            self._started = was_started
        else:
            self._started = False
            self._reset_safety_state()

        if errors:
            raise BaseExceptionGroup(
                f"PeerChannel '{self._config.name}' cleanup failed",
                errors,
            )

    def _forward(self, event: Event) -> bool:
        """Enqueue without holding up the shared EventBus consumer."""
        return self._enqueue(event)

    def _enqueue(self, event: Event, completion=None) -> bool:
        if not self._started:
            return False
        if not self.is_connected:
            if (event.type == EventType.CommandResult
                    or CommandRequest.from_value(event.data) is not None):
                self._fail_outbound("peer is disconnected or unhealthy", event)
            return False
        try:
            self._outbound.put_nowait(_Outbound(
                event, self._transport.connection_token, completion,
            ))
        except asyncio.QueueFull:
            self._fail_outbound("outbound queue is full", event)
            return False
        return True

    async def send(self, event: Event) -> bool:
        """Wait for one allow-listed event's ordered socket write.

        Terminal results share the same queue as ordinary traffic, so shutdown
        can confirm the write without overtaking an earlier output event.
        """
        if event.type not in self._config.routes.outbound:
            logger.warning(
                "PeerChannel '%s': outbound event type %s not in route white-list; dropped",
                self._config.name,
                event.type,
            )
            return False
        completion = asyncio.get_running_loop().create_future()
        if not self._enqueue(event, completion):
            return False
        try:
            return await asyncio.shield(completion)
        except asyncio.CancelledError:
            self._fail_outbound("outbound send was cancelled")
            raise

    @staticmethod
    def _finish(item: _Outbound, sent: bool) -> None:
        if item.completion is not None and not item.completion.done():
            item.completion.set_result(sent)

    def _discard_outbound(self) -> list[Event]:
        discarded = []
        while not self._outbound.empty():
            item = self._outbound.get_nowait()
            if item is not None:
                discarded.append(item.event)
                self._finish(item, False)
        return discarded

    def _fail_outbound(self, reason: str, unsent: Event | None = None) -> None:
        if self._delivery_fault is not None:
            if unsent is not None and unsent.type != EventType.CommandResult:
                request = CommandRequest.from_value(unsent.data)
                if request is not None:
                    self._publish_delivery_fault(self._delivery_fault, [request.command_id])
            return
        self._delivery_fault = reason
        discarded = self._discard_outbound()
        if unsent is not None:
            discarded.append(unsent)
        unsent_ids = []
        for event in discarded:
            # A lost result describes an already executed command; it cannot be
            # reclassified as a command that never reached the peer.
            if event.type != EventType.CommandResult:
                request = CommandRequest.from_value(event.data)
                if request is not None:
                    unsent_ids.append(request.command_id)
        self._publish_delivery_fault(reason, unsent_ids)
        logger.warning("PeerChannel '%s' outbound failed: %s", self.name, reason)
        # A waiting/not-yet-started sender needs a wakeup.  An active write is
        # cancelled and conservatively remains execution-unknown.
        self._outbound.put_nowait(None)
        sender = self._sender_task
        if (self._in_flight is not None and sender is not None
                and sender is not asyncio.current_task()):
            sender.cancel()

    def _publish_delivery_fault(self, reason: str, unsent_ids: list[str]) -> None:
        self._bus.put_event(Event(EventType.PeerDeliveryFault, {
            "peer": self.name,
            "channel_id": self.channel_id,
            "reason": reason,
            "unsent_command_ids": unsent_ids,
        }))

    async def _send_outbound(self) -> None:
        try:
            while self._delivery_fault is None:
                item = await self._outbound.get()
                if item is None:
                    return
                self._in_flight = item
                try:
                    async with asyncio.timeout(_SEND_TIMEOUT_S):
                        sent = await self._transport.send(
                            item.event, connection_token=item.connection_token,
                        )
                except PeerConnectionChanged as exc:
                    self._fail_outbound(str(exc), item.event)
                    return
                except Exception as exc:
                    self._fail_outbound(f"socket write failed: {exc or type(exc).__name__}")
                    return
                if not sent:
                    self._fail_outbound("socket write was not confirmed")
                    return
                if not self.is_ready:
                    return
                self._finish(item, True)
                self._in_flight = None
        finally:
            if self._in_flight is not None:
                self._finish(self._in_flight, False)
                self._in_flight = None
            self._discard_outbound()
            if self._delivery_fault is not None and self._transport_started:
                await self._transport.stop()
                self._transport_started = False

    def _harvest_sender(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("PeerChannel '%s' sender cleanup failed: %s", self.name, error)

    def _on_inbound(self, event: Event) -> bool:
        """Inbound: peer → engine. White-list before touching the bus."""
        if event.type in self._config.routes.inbound:
            self._bus.put_event(event)
            return True

        logger.warning(
            "PeerChannel '%s': inbound event type %s not in route white-list; dropped",
            self._config.name, event.type,
        )
        return False

    # ------------------------------------------------------------------
    # Safety: liveness tracking + watchdog
    # ------------------------------------------------------------------

    def _on_liveness(self) -> None:
        """Any received frame (incl. keep-alive) marks the peer alive."""
        self._last_seen = time.monotonic()
        self._stale_tripped = False  # re-arm the staleness alarm

    def _reset_safety_state(self) -> None:
        self._last_seen = None
        self._was_connected = False
        self._stale_tripped = False
        self._monitor_fault = None

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(_MONITOR_POLL_S)
            try:
                self._evaluate(time.monotonic())
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                # A dead watchdog must be reflected in channel health instead of
                # silently leaving a connected transport marked usable.
                self._monitor_fault = exc
                logger.error(
                    "PeerChannel '%s': watchdog monitor failed: %s",
                    self._config.name,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                return

    def _evaluate(self, now: float) -> bool:
        """
        Re-evaluate link health once. Returns True if a failsafe was tripped.
        Pure enough to unit-test directly (no sleeps/timers).
        """
        connected = self._transport.is_connected
        tripped = False

        # Fresh connection: reset the liveness clock so a just-connected peer
        # isn't immediately flagged stale.
        if connected and not self._was_connected:
            self._last_seen = now
            self._stale_tripped = False

        # Disconnect edge → trip once.  A heartbeat timeout already reported
        # the same outage, so the transport's later idle-close must not emit a
        # duplicate failsafe.
        disconnected = self._was_connected and not connected
        self._was_connected = connected
        if disconnected and not self._stale_tripped:
            # Mark the outage before invoking user-provided failsafe code.  Even
            # if that callback raises, health stays false and the next monitor
            # tick cannot duplicate the same transition.
            self._stale_tripped = True
            self._trip_failsafe("link lost")
            tripped = True

        # Heartbeat staleness while connected → trip once, re-armed by _on_liveness.
        timeout = self._config.watchdog_timeout_s
        if (connected and timeout is not None and self._last_seen is not None
                and not self._stale_tripped and (now - self._last_seen) > timeout):
            self._stale_tripped = True
            self._trip_failsafe(f"heartbeat timeout ({now - self._last_seen:.3f}s)")
            tripped = True

        return tripped

    def _trip_failsafe(self, reason: str) -> None:
        logger.warning("PeerChannel '%s' failsafe: %s", self._config.name, reason)
        if self._config.failsafe is None:
            return
        for event in self._config.failsafe():
            self._bus.put_event(event)
