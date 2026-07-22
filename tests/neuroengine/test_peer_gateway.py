"""
Tests for the PeerGateway routing layer (Phase 1 of the comms refactor).

Covers:
  - PeerChannel outbound: subscribed types are forwarded to the transport.
  - PeerChannel inbound: white-list — allowed types reach the bus, others dropped.
  - start/stop: registers and unregisters the outbound listeners; sets inbound hook.
  - PeerGateway: duplicate peer rejected; start/stop fan out to channels.
  - stimulation_peer_config: preserves the legacy push set + allows StimulationAck.
"""
from __future__ import annotations

import asyncio

import pytest

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.neuroengine.gateway.routing import RouteSpec, PeerConfig
from omnibci.infrastructure.neuroengine.gateway.peer_channel import PeerChannel
from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
from omnibci.infrastructure.neuroengine.gateway.peers import stimulation_peer_config, STIMULATION


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self) -> None:
        self.sent = []
        self.inbound_handler = None
        self.started = False
        self.stopped = False
        self._connected = True
        self.lifecycle = []

    def set_inbound_handler(self, handler) -> None:
        self.inbound_handler = handler

    def set_liveness_handler(self, handler) -> None:
        self.liveness_handler = handler

    async def start(self) -> None:
        self.lifecycle.append("start")
        self.started = True

    async def stop(self) -> None:
        self.lifecycle.append("stop")
        self.stopped = True

    async def send(self, event) -> None:
        self.sent.append(event)

    @property
    def is_connected(self) -> bool:
        return self._connected


class FakeBus:
    def __init__(self) -> None:
        self.listeners = {}
        self.put = []

    def register_event_listener(self, event_type, handler) -> None:
        self.listeners.setdefault(event_type, []).append(handler)

    def unregister_event_listener(self, event_type, handler) -> None:
        if event_type in self.listeners and handler in self.listeners[event_type]:
            self.listeners[event_type].remove(handler)

    def put_event(self, event) -> None:
        self.put.append(event)


def _channel(outbound, inbound, transport=None, bus=None):
    transport = transport or FakeTransport()
    bus = bus or FakeBus()
    cfg = PeerConfig(
        name="test",
        transport=transport,
        routes=RouteSpec(outbound=frozenset(outbound), inbound=frozenset(inbound)),
    )
    return PeerChannel(cfg, bus), transport, bus


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

class TestOutbound:
    def test_forward_sends_to_transport(self):
        ch, transport, _ = _channel({EventType.TrialCompleted}, set())
        asyncio.run(ch._forward(Event(EventType.TrialCompleted, data=1)))
        assert len(transport.sent) == 1
        assert transport.sent[0].type == EventType.TrialCompleted

    def test_only_subscribed_types_registered(self):
        ch, _, bus = _channel(
            {EventType.TrialCompleted, EventType.StopStimulation}, set()
        )
        asyncio.run(_start(ch))
        assert set(bus.listeners.keys()) == {EventType.TrialCompleted, EventType.StopStimulation}
        # not subscribed → not routed out
        assert EventType.StartStream not in bus.listeners


# ---------------------------------------------------------------------------
# Inbound white-list
# ---------------------------------------------------------------------------

class TestInbound:
    def test_allowed_type_reaches_bus(self):
        ch, _, bus = _channel(set(), {EventType.StimulationAck})
        ch._on_inbound(Event(EventType.StimulationAck, data="ok"))
        assert len(bus.put) == 1
        assert bus.put[0].type == EventType.StimulationAck

    def test_disallowed_type_dropped(self):
        ch, _, bus = _channel(set(), {EventType.StimulationAck})
        ch._on_inbound(Event(EventType.StartStream, data="nope"))
        assert bus.put == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_sets_inbound_handler(self):
        ch, transport, _ = _channel({EventType.TrialCompleted}, {EventType.StimulationAck})
        asyncio.run(_start(ch))
        assert transport.inbound_handler == ch._on_inbound

    def test_stop_unregisters_listeners(self):
        ch, transport, bus = _channel({EventType.TrialCompleted}, set())

        async def _run():
            ch.start()
            await asyncio.sleep(0)
            await ch.stop()

        asyncio.run(_run())
        assert bus.listeners.get(EventType.TrialCompleted) == []
        assert transport.stopped is True

    def test_immediate_stop_waits_for_scheduled_transport_start(self):
        ch, transport, _ = _channel(set(), set())

        async def _run():
            ch.start()
            await ch.stop()

        asyncio.run(_run())
        assert transport.lifecycle == ["start", "stop"]


# ---------------------------------------------------------------------------
# PeerGateway
# ---------------------------------------------------------------------------

class TestPeerGateway:
    def test_duplicate_peer_rejected(self):
        gw = PeerGateway(FakeBus())
        cfg = PeerConfig(name="dup", transport=FakeTransport(),
                         routes=RouteSpec(frozenset(), frozenset()))
        gw.add_peer(cfg)
        with pytest.raises(ValueError):
            gw.add_peer(cfg)

    def test_peer_lookup(self):
        gw = PeerGateway(FakeBus())
        cfg = PeerConfig(name="x", transport=FakeTransport(),
                         routes=RouteSpec(frozenset(), frozenset()))
        gw.add_peer(cfg)
        assert gw.peer("x").name == "x"


# ---------------------------------------------------------------------------
# stimulation routing table
# ---------------------------------------------------------------------------

class TestStimulationConfig:
    def test_preserves_legacy_outbound_and_allows_ack(self):
        cfg = stimulation_peer_config(host="127.0.0.1", port=8000)
        assert cfg.name == STIMULATION
        assert cfg.routes.outbound == frozenset({
            EventType.TrialCompleted, EventType.StartStimTrial, EventType.StopStimulation,
        })
        assert EventType.StimulationAck in cfg.routes.inbound


async def _start(channel) -> None:
    channel.start()
    await asyncio.sleep(0)


if __name__ == "__main__":
    pytest.main(["-s", "-v", __file__])
