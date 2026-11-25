"""
Tests for the PeerGateway routing layer (Phase 1 of the comms refactor).

Covers:
  - PeerChannel outbound: subscribed types are forwarded to the transport.
  - PeerChannel inbound: white-list — allowed types reach the bus, others dropped.
  - start/stop: registers and unregisters the outbound listeners; sets inbound hook.
  - PeerGateway: duplicate peer rejected; start/stop fan out to channels.
  - stimulation_peer_config: routes stimulation commands/results and accepts StimulationAck.
"""
from __future__ import annotations

import asyncio

import pytest

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.routing import RouteSpec, PeerConfig
from omnibci.infrastructure.ipc.peer_channel import PeerChannel
from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
from omnibci.infrastructure.neuroengine.gateway.peers import stimulation_peer_config, STIMULATION


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTransport:
    def __init__(self, name="transport", lifecycle=None,
                 fail_start=False, fail_stop=False,
                 start_entered=None, start_release=None,
                 stop_entered=None, stop_release=None) -> None:
        self.name = name
        self.sent = []
        self.inbound_handler = None
        self.liveness_handler = None
        self.started = False
        self.stopped = False
        self._connected = True
        self.connection_token = object()
        self.lifecycle = lifecycle if lifecycle is not None else []
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.start_count = 0
        self.stop_count = 0
        self.start_entered = start_entered
        self.start_release = start_release
        self.stop_entered = stop_entered
        self.stop_release = stop_release

    def set_inbound_handler(self, handler) -> None:
        self.inbound_handler = handler

    def set_liveness_handler(self, handler) -> None:
        self.liveness_handler = handler

    async def start(self) -> None:
        self.lifecycle.append(f"start:{self.name}")
        self.start_count += 1
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            await self.start_release.wait()
        if self.fail_start:
            raise RuntimeError(f"start failed: {self.name}")
        self.started = True

    async def stop(self) -> None:
        self.lifecycle.append(f"stop:{self.name}")
        self.stop_count += 1
        if self.stop_entered is not None:
            self.stop_entered.set()
        if self.stop_release is not None:
            await self.stop_release.wait()
        if self.fail_stop:
            raise RuntimeError(f"stop failed: {self.name}")
        self.stopped = True

    async def send(self, event, *, connection_token=None) -> bool:
        self.sent.append(event)
        return True

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

        async def _run():
            await ch.start()
            sent = await ch.send(Event(EventType.TrialCompleted, data=1))
            await ch.stop()
            return sent

        sent = asyncio.run(_run())
        assert sent is True
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
        accepted = ch._on_inbound(Event(EventType.StimulationAck, data="ok"))
        assert accepted is True
        assert len(bus.put) == 1
        assert bus.put[0].type == EventType.StimulationAck

    def test_disallowed_type_dropped(self):
        ch, _, bus = _channel(set(), {EventType.StimulationAck})
        accepted = ch._on_inbound(Event(EventType.StartStream, data="nope"))
        assert accepted is False
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
            await ch.start()
            await ch.stop()

        asyncio.run(_run())
        assert bus.listeners.get(EventType.TrialCompleted) == []
        assert transport.stopped is True

    def test_start_then_stop_has_ordered_lifecycle(self):
        ch, transport, _ = _channel(set(), set())

        async def _run():
            await ch.start()
            await ch.stop()

        asyncio.run(_run())
        assert transport.lifecycle == ["start:transport", "stop:transport"]

    def test_start_and_stop_are_idempotent(self):
        ch, transport, bus = _channel({EventType.TrialCompleted}, set())

        async def _run():
            await ch.start()
            await ch.start()
            await ch.stop()
            await ch.stop()

        asyncio.run(_run())
        assert transport.start_count == 1
        assert transport.stop_count == 1
        assert bus.listeners[EventType.TrialCompleted] == []

    def test_wait_until_connected_is_a_stronger_barrier_than_start(self):
        transport = FakeTransport()
        transport._connected = False
        ch, _, _ = _channel(set(), set(), transport=transport)

        async def _run():
            await ch.start()

            async def connect_next_turn():
                await asyncio.sleep(0)
                transport._connected = True

            connect_task = asyncio.create_task(connect_next_turn())
            await ch.wait_until_connected(timeout_s=0.2)
            await connect_task
            await ch.stop()

        asyncio.run(_run())

    def test_wait_until_connected_times_out_without_link(self):
        transport = FakeTransport()
        transport._connected = False
        ch, _, _ = _channel(set(), set(), transport=transport)

        async def _run():
            await ch.start()
            with pytest.raises(TimeoutError, match="did not connect"):
                await ch.wait_until_connected(timeout_s=0.001)
            await ch.stop()

        asyncio.run(_run())

    def test_wait_until_connected_rejects_unstarted_channel(self):
        ch, _, _ = _channel(set(), set())

        async def _run():
            with pytest.raises(RuntimeError, match="not cleanly started"):
                await ch.wait_until_connected(timeout_s=0.1)

        asyncio.run(_run())

    def test_failed_start_rolls_back_transport_listener_and_safety_state(self):
        transport = FakeTransport(fail_start=True)
        ch, _, bus = _channel({EventType.TrialCompleted}, set(), transport=transport)
        ch._last_seen = 12.0
        ch._was_connected = True
        ch._stale_tripped = True

        async def _run():
            with pytest.raises(RuntimeError, match="start failed"):
                await ch.start()

        asyncio.run(_run())
        assert transport.lifecycle == ["start:transport", "stop:transport"]
        assert bus.listeners[EventType.TrialCompleted] == []
        assert ch._started is False
        assert ch._transport_started is False
        assert ch._last_seen is None
        assert ch._was_connected is False
        assert ch._stale_tripped is False

    def test_start_cancellation_finishes_channel_rollback(self):
        async def _run():
            entered = asyncio.Event()
            release = asyncio.Event()
            transport = FakeTransport(
                start_entered=entered,
                start_release=release,
            )
            ch, _, bus = _channel(
                {EventType.TrialCompleted}, set(), transport=transport,
            )

            task = asyncio.create_task(ch.start())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert bus.listeners[EventType.TrialCompleted] == []
            assert transport.stop_count == 1
            assert ch._started is False
            assert ch.is_dirty is False
            assert ch.has_lifecycle_resources is False

        asyncio.run(_run())

    def test_stop_cancellation_waits_for_channel_cleanup(self):
        async def _run():
            entered = asyncio.Event()
            release = asyncio.Event()
            transport = FakeTransport(
                stop_entered=entered,
                stop_release=release,
            )
            ch, _, bus = _channel(
                {EventType.TrialCompleted}, set(), transport=transport,
            )
            await ch.start()

            task = asyncio.create_task(ch.stop())
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert task.done() is False
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert bus.listeners[EventType.TrialCompleted] == []
            assert ch._started is False
            assert ch.is_dirty is False
            assert ch.has_lifecycle_resources is False

        asyncio.run(_run())


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

    def test_add_peer_is_rejected_once_startup_has_begun(self):
        gw = PeerGateway(FakeBus())
        gw.add_peer(PeerConfig(
            name="x",
            transport=FakeTransport(name="x"),
            routes=RouteSpec(frozenset(), frozenset()),
        ))

        asyncio.run(gw.start())
        with pytest.raises(RuntimeError, match="after gateway startup"):
            gw.add_peer(PeerConfig(
                name="y",
                transport=FakeTransport(name="y"),
                routes=RouteSpec(frozenset(), frozenset()),
            ))
        asyncio.run(gw.stop())

        # Configuration remains frozen after a completed stop; restart uses the
        # same channel topology that was originally made transactional.
        with pytest.raises(RuntimeError, match="after gateway startup"):
            gw.add_peer(PeerConfig(
                name="z",
                transport=FakeTransport(name="z"),
                routes=RouteSpec(frozenset(), frozenset()),
            ))

    def test_start_and_stop_are_async_and_idempotent(self):
        transport = FakeTransport(name="x")
        gw = PeerGateway(FakeBus())
        gw.add_peer(PeerConfig(
            name="x",
            transport=transport,
            routes=RouteSpec(frozenset(), frozenset()),
        ))

        async def _run():
            await gw.start()
            await gw.start()
            await gw.stop()
            await gw.stop()

        asyncio.run(_run())
        assert transport.start_count == 1
        assert transport.stop_count == 1

    def test_partial_start_failure_rolls_back_in_reverse_order(self):
        lifecycle = []
        gw = PeerGateway(FakeBus())
        for name, fail in (("a", False), ("b", False), ("c", True)):
            gw.add_peer(PeerConfig(
                name=name,
                transport=FakeTransport(
                    name=name,
                    lifecycle=lifecycle,
                    fail_start=fail,
                ),
                routes=RouteSpec(frozenset(), frozenset()),
            ))

        async def _run():
            with pytest.raises(RuntimeError, match="start failed: c"):
                await gw.start()

        asyncio.run(_run())
        # The failing channel cleans itself first; the gateway then unwinds the
        # successfully-started prefix in reverse order.
        assert lifecycle == [
            "start:a", "start:b", "start:c",
            "stop:c", "stop:b", "stop:a",
        ]
        assert gw._started is False

    def test_failed_current_channel_cleanup_is_reported_and_remains_dirty(self):
        lifecycle = []
        transport = FakeTransport(
            name="x",
            lifecycle=lifecycle,
            fail_start=True,
            fail_stop=True,
        )
        gw = PeerGateway(FakeBus())
        gw.add_peer(PeerConfig(
            name="x",
            transport=transport,
            routes=RouteSpec(frozenset(), frozenset()),
        ))

        async def _run():
            with pytest.raises(ExceptionGroup) as raised:
                await gw.start()

            # PeerChannel makes the first rollback attempt; PeerGateway still
            # explicitly retries the currently-failing channel.
            assert lifecycle == ["start:x", "stop:x", "stop:x"]
            assert raised.value.subgroup(RuntimeError) is not None
            assert gw._started is False
            assert gw._dirty is True
            assert gw.peer("x").is_dirty is True

            transport.fail_stop = False
            await gw.stop()
            assert gw._started is False
            assert gw._dirty is False
            assert gw.peer("x").has_lifecycle_resources is False

        asyncio.run(_run())

    def test_start_cancellation_rolls_back_current_and_prior_channels(self):
        async def _run():
            lifecycle = []
            entered = asyncio.Event()
            release = asyncio.Event()
            gw = PeerGateway(FakeBus())
            for name in ("a", "b"):
                gw.add_peer(PeerConfig(
                    name=name,
                    transport=FakeTransport(
                        name=name,
                        lifecycle=lifecycle,
                        start_entered=entered if name == "b" else None,
                        start_release=release if name == "b" else None,
                    ),
                    routes=RouteSpec(frozenset(), frozenset()),
                ))

            task = asyncio.create_task(gw.start())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert lifecycle == ["start:a", "start:b", "stop:b", "stop:a"]
            assert gw._started is False
            assert gw._dirty is False
            assert all(
                not channel.has_lifecycle_resources
                for channel in gw._channels.values()
            )

        asyncio.run(_run())

    def test_stop_attempts_every_channel_when_one_fails(self):
        lifecycle = []
        transports = {}
        gw = PeerGateway(FakeBus())
        for name in ("a", "b", "c"):
            transport = FakeTransport(name=name, lifecycle=lifecycle)
            transports[name] = transport
            gw.add_peer(PeerConfig(
                name=name,
                transport=transport,
                routes=RouteSpec(frozenset(), frozenset()),
            ))

        async def _run():
            await gw.start()
            lifecycle.clear()
            transports["b"].fail_stop = True
            with pytest.raises(ExceptionGroup) as raised:
                await gw.stop()

            assert raised.value.subgroup(RuntimeError) is not None
            assert gw._started is True
            assert gw._dirty is True

            transports["b"].fail_stop = False
            await gw.stop()
            assert gw._started is False
            assert gw._dirty is False

        asyncio.run(_run())
        assert lifecycle[:3] == ["stop:c", "stop:b", "stop:a"]
        assert lifecycle[3:] == ["stop:b"]
        assert transports["a"].stopped is True
        assert transports["c"].stopped is True
        assert gw._started is False

    def test_stop_rejects_silent_channel_resource_leak(self):
        async def _run():
            gw = PeerGateway(FakeBus())
            gw.add_peer(PeerConfig(
                name="x",
                transport=FakeTransport(name="x"),
                routes=RouteSpec(frozenset(), frozenset()),
            ))
            await gw.start()
            channel = gw.peer("x")
            real_stop = channel.stop

            async def _broken_stop():
                return None

            channel.stop = _broken_stop
            with pytest.raises(
                ExceptionGroup,
                match="PeerGateway cleanup failed",
            ) as raised:
                await gw.stop()

            assert raised.value.subgroup(RuntimeError) is not None
            assert gw._started is True
            assert gw._dirty is True

            channel.stop = real_stop
            await gw.stop()
            assert gw._started is False
            assert gw._dirty is False

        asyncio.run(_run())

    def test_stop_cancellation_cleans_every_channel_before_propagating(self):
        async def _run():
            lifecycle = []
            entered = asyncio.Event()
            release = asyncio.Event()
            gw = PeerGateway(FakeBus())
            for name in ("a", "b", "c"):
                gw.add_peer(PeerConfig(
                    name=name,
                    transport=FakeTransport(
                        name=name,
                        lifecycle=lifecycle,
                        stop_entered=entered if name == "c" else None,
                        stop_release=release if name == "c" else None,
                    ),
                    routes=RouteSpec(frozenset(), frozenset()),
                ))

            await gw.start()
            lifecycle.clear()
            task = asyncio.create_task(gw.stop())
            await entered.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

            assert lifecycle == ["stop:c", "stop:b", "stop:a"]
            assert gw._started is False
            assert gw._dirty is False
            assert all(
                not channel.has_lifecycle_resources
                for channel in gw._channels.values()
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# stimulation routing table
# ---------------------------------------------------------------------------

class TestStimulationConfig:
    def test_routes_stimulation_commands_results_and_ack(self):
        cfg = stimulation_peer_config(host="127.0.0.1", port=8000)
        assert cfg.name == STIMULATION
        assert cfg.routes.outbound == frozenset({
            EventType.TrialCompleted, EventType.StartStimTrial, EventType.StopStimulation,
            EventType.PauseStimulation,
        })
        assert EventType.StimulationAck in cfg.routes.inbound
        assert EventType.CommandResult in cfg.routes.inbound


async def _start(channel) -> None:
    await channel.start()


def _safety_channel(transport, failsafe=None, watchdog=None):
    bus = FakeBus()
    fault = [Event(EventType.ActuationFault, {"code": "LINK_LOST"})]
    failsafe_factory = failsafe if callable(failsafe) else (
        (lambda: fault) if failsafe else None
    )
    cfg = PeerConfig(
        name="actuation", transport=transport,
        routes=RouteSpec(frozenset({EventType.ActuationCommand}),
                         frozenset({EventType.ActuationFault})),
        failsafe=failsafe_factory,
        watchdog_timeout_s=watchdog,
    )
    return PeerChannel(cfg, bus), bus


class TestDisconnectEdge:
    def test_disconnect_trips_failsafe_once(self):
        t = FakeTransport()
        t._connected = True
        ch, bus = _safety_channel(t, failsafe=True, watchdog=None)
        ch._evaluate(now=1.0)
        assert bus.put == []
        t._connected = False
        ch._evaluate(now=2.0)
        assert len(bus.put) == 1 and bus.put[0].type == EventType.ActuationFault
        ch._evaluate(now=3.0)
        assert len(bus.put) == 1

    def test_no_trip_before_first_connection(self):
        t = FakeTransport()
        t._connected = False
        ch, bus = _safety_channel(t, failsafe=True, watchdog=1.0)
        for now in (1.0, 2.0, 5.0):
            ch._evaluate(now)
        assert bus.put == []


class TestHeartbeatStaleness:
    def test_stale_trips_then_rearms(self):
        t = FakeTransport()
        t._connected = True
        ch, bus = _safety_channel(t, failsafe=True, watchdog=1.0)
        ch._started = True
        ch._evaluate(now=10.0)
        assert ch.is_connected is True
        ch._evaluate(now=10.5)
        assert bus.put == []
        ch._evaluate(now=11.5)
        assert len(bus.put) == 1
        assert ch.is_connected is False

        ch._evaluate(now=12.0)
        assert len(bus.put) == 1
        ch._on_liveness()
        assert ch.is_connected is True
        ch._evaluate(now=12.1)
        assert len(bus.put) == 1

    def test_idle_disconnect_after_stale_does_not_duplicate_failsafe(self):
        t = FakeTransport()
        t._connected = True
        ch, bus = _safety_channel(t, failsafe=True, watchdog=1.0)
        ch._started = True
        ch._evaluate(now=1.0)
        ch._evaluate(now=2.1)
        assert len(bus.put) == 1
        assert ch.is_connected is False

        t._connected = False
        ch._evaluate(now=3.0)
        ch._evaluate(now=4.0)
        assert len(bus.put) == 1
        assert ch.is_connected is False

    def test_failsafe_exception_cannot_leave_stale_channel_healthy(self):
        def _broken_failsafe():
            raise RuntimeError("failsafe callback failed")

        t = FakeTransport()
        t._connected = True
        ch, _ = _safety_channel(t, failsafe=_broken_failsafe, watchdog=1.0)
        ch._started = True
        ch._evaluate(now=1.0)

        try:
            ch._evaluate(now=2.1)
        except RuntimeError as exc:
            assert str(exc) == "failsafe callback failed"
        else:
            raise AssertionError("broken failsafe must remain observable")

        assert ch._stale_tripped is True
        assert ch.is_connected is False

    def test_monitor_fault_is_recorded_and_marks_channel_unhealthy(self, monkeypatch):
        from omnibci.infrastructure.ipc import peer_channel as peer_channel_module

        def _broken_failsafe():
            raise RuntimeError("monitor failed")

        async def _run():
            t = FakeTransport()
            t._connected = True
            ch, _ = _safety_channel(t, failsafe=_broken_failsafe, watchdog=0.01)
            ch._started = True
            ch._was_connected = True
            ch._last_seen = 0.0
            await ch._monitor()
            return ch

        monkeypatch.setattr(peer_channel_module, "_MONITOR_POLL_S", 0)
        ch = asyncio.run(_run())
        assert isinstance(ch.monitor_fault, RuntimeError)
        assert str(ch.monitor_fault) == "monitor failed"
        assert ch.is_connected is False

    def test_fresh_connection_resets_clock(self):
        t = FakeTransport()
        t._connected = False
        ch, bus = _safety_channel(t, failsafe=True, watchdog=1.0)
        ch._evaluate(now=100.0)
        t._connected = True
        ch._evaluate(now=100.0)
        ch._evaluate(now=100.5)
        assert bus.put == []


class TestUnmonitored:
    def test_no_failsafe_no_watchdog_is_not_monitored(self):
        t = FakeTransport()
        t._connected = True
        ch, bus = _safety_channel(t, failsafe=None, watchdog=None)
        assert ch._monitored is False
        t._connected = False
        ch._evaluate(now=1.0)
        assert bus.put == []


class TestActuationConfigSafety:
    def test_actuation_config_has_failsafe_and_watchdog(self):
        from omnibci.infrastructure.neuroengine.gateway.peers import ACTUATION_LINK_LOST, actuation_peer_config

        cfg = actuation_peer_config(host="127.0.0.1", port=8001, watchdog_timeout_s=2.5)
        assert cfg.watchdog_timeout_s == 2.5
        assert cfg.transport._server.idle_timeout == 5.0
        assert cfg.failsafe is not None
        events = list(cfg.failsafe())
        assert len(events) == 1
        assert events[0].type == EventType.ActuationFault
        assert events[0].data == ACTUATION_LINK_LOST


import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from omnibci.application.plugin_management.execution import bindings_fingerprint
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.ipc.peer_transport import ListenTransport
from omnibci.infrastructure.neuroengine import builder
from omnibci.infrastructure.neuroengine.gateway.peers import actuation_peer_config
from tests.execution_helpers import output_bindings


@pytest_asyncio.fixture
async def build_graph(monkeypatch):
    monkeypatch.setattr(builder.mp, "Queue", MagicMock())
    monkeypatch.setattr(builder.mp, "Pipe", lambda **kwargs: (MagicMock(), MagicMock()))
    monkeypatch.setattr(builder, "AcquisitionWorker", MagicMock())
    monkeypatch.setattr(builder, "SignalProcessingWorker", MagicMock())
    monkeypatch.setattr(ListenTransport, "start", AsyncMock())
    monkeypatch.setattr(ListenTransport, "stop", AsyncMock())
    engines = []

    async def build(**settings):
        snapshot = {"CONFIG.module_revisions": {role: "a" * 64 for role in (
            "acquisition", "processing", "stimulation", "actuation")},
            "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "run-1", "PLUGINS.bindings": output_bindings(), **settings}
        engine = builder.NeuroEngineBuilder(settings_snapshot=snapshot).build()
        assert not engine.peer_gateway._channels
        engine._loop = asyncio.get_running_loop()
        engine._started = True
        await engine.peer_gateway.start()
        engines.append(engine)
        return engine, snapshot
    yield build
    for engine in engines:
        await engine.peer_gateway.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stim_managed,actuation_managed", [
    (True, True), (False, True), (True, False), (False, False),
])
async def test_prepared_listener_uses_its_own_external_identity(
    build_graph, stim_managed, actuation_managed,
):
    engine, snapshot = await build_graph(**{
        "STIMULATION.auto_spawn": stim_managed,
        "ACTUATION.auto_spawn": actuation_managed,
    })
    for role, managed in (("stimulation", stim_managed), ("actuation", actuation_managed)):
        await engine.ensure_output_ready(role, snapshot)
        server = engine.peer_gateway.peer(role)._transport._server
        expected = None if managed else PeerIdentity(role, "a" * 64, "c" * 64, "run-1",
                                                    engine.node_operation_id(role), bindings_fingerprint(output_bindings(), role))
        assert server.identity == expected
        assert server.max_clients == (None if role == "stimulation" and managed else 1)


@pytest.mark.asyncio
async def test_preparation_is_idempotent_and_does_not_prepare_another_role(build_graph):
    engine, snapshot = await build_graph()
    await engine.ensure_output_ready("stimulation", snapshot)
    channel = engine.peer_gateway.peer("stimulation")
    await engine.ensure_output_ready("stimulation", snapshot)
    assert engine.peer_gateway.peer("stimulation") is channel
    assert "actuation" not in engine.peer_gateway._channels
    assert channel._transport._server.identity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("settings,address", [
    ({}, ("127.0.0.1", 8765)),
    ({"STIMULATION.server_ip": "192.0.2.4", "STIMULATION.server_port": 9234}, ("192.0.2.4", 9234)),
])
async def test_stimulation_listener_and_client_use_the_same_module_address(build_graph, settings, address):
    from omnibci.infrastructure.stimulation.application import Stimulation

    engine, snapshot = await build_graph(**settings)
    await engine.ensure_output_ready("stimulation", snapshot)
    server = engine.peer_gateway.peer("stimulation")._transport._server
    client = Stimulation(settings_snapshot=snapshot).peer_channel._transport._client
    assert (server.host, server.port) == address
    assert (client.server_ip, client.server_port) == address


@pytest.mark.asyncio
async def test_external_actuation_is_prepared_before_module_activation(build_graph):
    engine, snapshot = await build_graph(**{"ACTUATION.auto_spawn": False})
    assert not engine.peer_gateway._channels
    await engine.ensure_output_ready("actuation", snapshot)
    assert engine.peer_gateway.peer("actuation")._transport._server.identity == PeerIdentity(
        "actuation", "a" * 64, "c" * 64, "run-1",
        engine.node_operation_id("actuation"), bindings_fingerprint(output_bindings(), "actuation"))
    assert not engine._actuation_active


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["stimulation", "actuation"])
@pytest.mark.parametrize("missing", ["CONFIG.module_revisions", "CONFIG.engine_revision", "CONFIG.generation"])
async def test_invalid_external_identity_fails_when_preparing_only_that_role(build_graph, role, missing):
    engine, snapshot = await build_graph(**{f"{role.upper()}.auto_spawn": False, missing: None})
    with pytest.raises(ValueError, match="Peer identity"):
        await engine.ensure_output_ready(role, snapshot)
    assert not engine.peer_gateway._channels
    builder.AcquisitionWorker.assert_not_called()
    builder.SignalProcessingWorker.assert_not_called()


def test_peer_factories_preserve_routing_while_limiting_external_stimulation():
    identity = PeerIdentity("stimulation", "a" * 64, "c" * 64, "run-1", "prepare-1", "d" * 64)
    managed = stimulation_peer_config("127.0.0.1", 8000)
    external = stimulation_peer_config("127.0.0.1", 8000, identity=identity)
    assert managed.routes == external.routes
    assert managed.transport._server.max_clients is None
    assert external.transport._server.max_clients == 1

    identity = PeerIdentity("actuation", "a" * 64, "c" * 64, "run-1", "prepare-1", "d" * 64)
    managed = actuation_peer_config("127.0.0.1", 8001, watchdog_timeout_s=2)
    external = actuation_peer_config("127.0.0.1", 8001, watchdog_timeout_s=2, identity=identity)
    assert managed.routes == external.routes
    assert managed.transport._server.max_clients == external.transport._server.max_clients == 1
    assert managed.transport._server.idle_timeout == external.transport._server.idle_timeout == 4


@pytest.mark.asyncio
async def test_replacing_a_listener_invalidates_its_operation_even_with_same_configuration(build_graph):
    engine, snapshot = await build_graph(**{"ACTUATION.auto_spawn": False})
    assert engine.node_operation_id("actuation") is None
    await engine.ensure_output_ready("actuation", snapshot)
    first = engine.node_operation_id("actuation")
    assert first
    await engine.ensure_output_ready("actuation", snapshot)
    assert engine.node_operation_id("actuation") == first
    await engine.reconfigure_output_modules(snapshot, ("actuation",))
    assert engine.node_operation_id("actuation") not in (None, first)
    await engine.peer_gateway.stop()
    assert engine.node_operation_id("actuation") is None


if __name__ == "__main__":
    pytest.main(["-s", "-v", __file__])
