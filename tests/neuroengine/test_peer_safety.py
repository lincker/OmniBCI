"""
Tests for Phase 4: PeerChannel connection/heartbeat watchdog + failsafe.

The monitor's per-tick logic lives in PeerChannel._evaluate(now), which is pure
enough to drive deterministically (no sleeps). We feed a fake transport whose
is_connected we toggle, and a controllable clock.

Covers:
  - disconnect edge trips the failsafe once;
  - heartbeat staleness trips once and re-arms after a liveness signal;
  - a fresh connection resets the liveness clock (no false stale trip);
  - no failsafe/watchdog configured → not "monitored", nothing trips;
  - effector_peer_config wires the LINK_LOST failsafe + watchdog.
"""
from __future__ import annotations

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.neuroengine.gateway.routing import RouteSpec, PeerConfig
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.neuroengine.gateway.peers import (
    effector_peer_config, EFFECTOR_LINK_LOST,
)


class FakeTransport:
    def __init__(self, connected=False):
        self._connected = connected
        self.inbound_handler = None
        self.liveness_handler = None

    def set_inbound_handler(self, h): self.inbound_handler = h
    def set_liveness_handler(self, h): self.liveness_handler = h
    async def start(self): pass
    async def stop(self): pass
    async def send(self, e): pass

    @property
    def is_connected(self): return self._connected


class FakeBus:
    def __init__(self): self.put = []
    def register_event_listener(self, t, h): pass
    def unregister_event_listener(self, t, h): pass
    def put_event(self, e): self.put.append(e)


def _channel(transport, failsafe=None, watchdog=None):
    bus = FakeBus()
    fault = [Event(EventType.EffectorFault, {"code": "LINK_LOST"})]
    cfg = PeerConfig(
        name="effector", transport=transport,
        routes=RouteSpec(frozenset({EventType.EffectorCommand}),
                         frozenset({EventType.EffectorFault})),
        failsafe=(lambda: fault) if failsafe else None,
        watchdog_timeout_s=watchdog,
    )
    return PeerChannel(cfg, bus), bus


class TestDisconnectEdge:
    def test_disconnect_trips_failsafe_once(self):
        t = FakeTransport(connected=True)
        ch, bus = _channel(t, failsafe=True, watchdog=None)
        ch._evaluate(now=1.0)           # connected → arm
        assert bus.put == []
        t._connected = False
        ch._evaluate(now=2.0)           # edge → trip
        assert len(bus.put) == 1 and bus.put[0].type == EventType.EffectorFault
        ch._evaluate(now=3.0)           # still down, no re-trip
        assert len(bus.put) == 1

    def test_no_trip_before_first_connection(self):
        t = FakeTransport(connected=False)
        ch, bus = _channel(t, failsafe=True, watchdog=1.0)
        for now in (1.0, 2.0, 5.0):
            ch._evaluate(now)
        assert bus.put == []            # never connected → never an edge / stale


class TestHeartbeatStaleness:
    def test_stale_trips_then_rearms(self):
        t = FakeTransport(connected=True)
        ch, bus = _channel(t, failsafe=True, watchdog=1.0)
        ch._evaluate(now=10.0)          # connect → last_seen=10
        ch._evaluate(now=10.5)          # within window
        assert bus.put == []
        ch._evaluate(now=11.5)          # >1s since last_seen → trip
        assert len(bus.put) == 1
        ch._evaluate(now=12.0)          # already tripped, no spam
        assert len(bus.put) == 1
        ch._on_liveness()               # a frame arrives → re-arm + refresh clock
        ch._evaluate(now=12.1)          # healthy again
        assert len(bus.put) == 1

    def test_fresh_connection_resets_clock(self):
        t = FakeTransport(connected=False)
        ch, bus = _channel(t, failsafe=True, watchdog=1.0)
        ch._evaluate(now=100.0)         # not connected
        t._connected = True
        ch._evaluate(now=100.0)         # just connected → last_seen=100, not stale
        ch._evaluate(now=100.5)
        assert bus.put == []


class TestUnmonitored:
    def test_no_failsafe_no_watchdog_is_not_monitored(self):
        t = FakeTransport(connected=True)
        ch, bus = _channel(t, failsafe=None, watchdog=None)
        assert ch._monitored is False
        # even if evaluated, disconnect logs but has no failsafe events
        t._connected = False
        ch._evaluate(now=1.0)
        assert bus.put == []


class TestEffectorConfigSafety:
    def test_effector_config_has_failsafe_and_watchdog(self):
        cfg = effector_peer_config(host="127.0.0.1", port=8001, watchdog_timeout_s=2.5)
        assert cfg.watchdog_timeout_s == 2.5
        assert cfg.failsafe is not None
        events = list(cfg.failsafe())
        assert len(events) == 1
        assert events[0].type == EventType.EffectorFault
        assert events[0].data == EFFECTOR_LINK_LOST
