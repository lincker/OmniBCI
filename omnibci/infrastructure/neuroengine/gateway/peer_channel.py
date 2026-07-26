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
from typing import Optional

from omnibci.shared.events import Event
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig

logger = logging.getLogger(__name__)

# How often the monitor re-evaluates link health, in seconds.
_MONITOR_POLL_S = 0.1


class PeerChannel:
    def __init__(self, config: PeerConfig, bus: EventBus) -> None:
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

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def is_connected(self) -> bool:
        """True only while the channel is started and the peer is healthy."""
        return (
            self._started
            and not self._stale_tripped
            and self._monitor_fault is None
            and self._transport.is_connected
        )

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
            if self._started and not self._dirty:
                return

            # A previous stop may have failed after completing only part of its
            # cleanup.  Finish that cleanup before attempting a fresh start.
            if self.has_lifecycle_resources:
                await self._cleanup_shielded()

            self._reset_safety_state()
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

        if self._transport_started:
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

    async def _forward(self, event: Event) -> bool:
        """Outbound: engine → peer. Only subscribed types reach here."""
        if not self.is_connected:
            logger.warning(
                "PeerChannel '%s': peer is disconnected or unhealthy; outbound event dropped",
                self._config.name,
            )
            return False
        return await self._transport.send(event)

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
