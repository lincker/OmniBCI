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
from __future__ import annotations

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
        self._monitor_task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def _monitored(self) -> bool:
        return (self._config.failsafe is not None
                or self._config.watchdog_timeout_s is not None)

    def start(self) -> None:
        """
        Wire routing + safety and bring the link up. Sync (mirrors the old
        ServerMessageManagement.start): registers bus listeners, installs the
        liveness hook, schedules the transport's serve/connect loop, and (when a
        failsafe/watchdog is configured) starts the monitor. Must be called from
        within a running event loop.
        """
        self._transport.set_inbound_handler(self._on_inbound)
        self._transport.set_liveness_handler(self._on_liveness)
        for event_type in self._config.routes.outbound:
            self._bus.register_event_listener(event_type, self._forward)
        asyncio.create_task(self._transport.start())
        if self._monitored:
            self._monitor_task = asyncio.create_task(self._monitor())
        logger.info(
            "PeerChannel '%s' up: out=%s in=%s watchdog=%s",
            self._config.name,
            sorted(int(t) for t in self._config.routes.outbound),
            sorted(int(t) for t in self._config.routes.inbound),
            self._config.watchdog_timeout_s,
        )

    async def stop(self) -> None:
        # Cancel the monitor first so a clean shutdown doesn't look like a fault.
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._monitor_task = None
        for event_type in self._config.routes.outbound:
            self._bus.unregister_event_listener(event_type, self._forward)
        await self._transport.stop()

    async def _forward(self, event: Event) -> None:
        """Outbound: engine → peer. Only subscribed types reach here."""
        await self._transport.send(event)

    def _on_inbound(self, event: Event) -> None:
        """Inbound: peer → engine. White-list before touching the bus."""
        if event.type in self._config.routes.inbound:
            self._bus.put_event(event)
        else:
            logger.warning(
                "PeerChannel '%s': inbound event type %s not in route white-list; dropped",
                self._config.name, event.type,
            )

    # ------------------------------------------------------------------
    # Safety: liveness tracking + watchdog
    # ------------------------------------------------------------------

    def _on_liveness(self) -> None:
        """Any received frame (incl. keep-alive) marks the peer alive."""
        self._last_seen = time.monotonic()
        self._stale_tripped = False  # re-arm the staleness alarm

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(_MONITOR_POLL_S)
            self._evaluate(time.monotonic())

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

        # Disconnect edge → trip once.
        if self._was_connected and not connected:
            self._trip_failsafe("link lost")
            tripped = True

        self._was_connected = connected

        # Heartbeat staleness while connected → trip once, re-armed by _on_liveness.
        timeout = self._config.watchdog_timeout_s
        if (connected and timeout is not None and self._last_seen is not None
                and not self._stale_tripped and (now - self._last_seen) > timeout):
            self._trip_failsafe(f"heartbeat timeout ({now - self._last_seen:.3f}s)")
            self._stale_tripped = True
            tripped = True

        return tripped

    def _trip_failsafe(self, reason: str) -> None:
        logger.warning("PeerChannel '%s' failsafe: %s", self._config.name, reason)
        if self._config.failsafe is None:
            return
        for event in self._config.failsafe():
            self._bus.put_event(event)
