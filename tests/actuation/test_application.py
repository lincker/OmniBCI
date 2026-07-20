"""
Actuation application loop: command → play + Ack; play failure → Fault and
the loop survives; heartbeat publishes ActuationState; failed setup exits
with a Fault as its last word.
"""
import asyncio

import pytest

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.actuation.application import Actuation


def _run(coro):
    return asyncio.run(coro)


class _SpyBus:
    """Records put_event; register/start/stop are no-ops."""

    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


class _FakeActuator:
    def __init__(self, fail_on=frozenset()):
        self.executed = []
        self.torn_down = False
        self._fail_on = fail_on

    def setup(self):
        pass

    def execute(self, command):
        if command.get("target_id") in self._fail_on:
            raise RuntimeError("boom")
        self.executed.append(dict(command))

    def teardown(self):
        self.torn_down = True


def _app(fail_on=frozenset()) -> Actuation:
    app = Actuation.__new__(Actuation)          # skip __init__: no settings/socket
    app.event_bus = _SpyBus()
    app.command_queue = __import__("queue").Queue()
    app.actuator = _FakeActuator(fail_on=fail_on)
    app._heartbeat_interval = 0.01
    app._max_command_age = 0.0        # age gate disabled by default (as in prod)
    app.should_stop = False
    return app


def _typed(bus, etype):
    return [e for e in bus.events if e.type == etype]


class TestCommandExecution:
    def test_command_is_executed_and_acked(self):
        app = _app()
        app._enqueue_command(Event(EventType.ActuationCommand,
                                   {"target_id": 5, "event_id": 2, "confidence": 0.8}))
        _run(app.execute_command())
        assert app.actuator.executed == [{"target_id": 5, "event_id": 2, "confidence": 0.8}]
        acks = _typed(app.event_bus, EventType.ActuationAck)
        assert len(acks) == 1 and acks[0].data["target_id"] == 5
        assert acks[0].data["status"] == "ok"

    def test_failed_command_faults_but_loop_survives(self):
        # Sequential arrivals (not a backlog): 7 fails, the loop survives and
        # the later 8 executes normally.
        app = _app(fail_on={7})
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 7}))
        _run(app.execute_command())
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 8}))
        _run(app.execute_command())
        faults = _typed(app.event_bus, EventType.ActuationFault)
        assert len(faults) == 1
        assert faults[0].data["code"] == "COMMAND_FAILED" and faults[0].data["target_id"] == 7
        # the next command still executed and acked
        assert app.actuator.executed == [{"target_id": 8}]
        assert len(_typed(app.event_bus, EventType.ActuationAck)) == 1

    def test_queued_failing_command_is_superseded_not_executed(self):
        # Backlog semantics: a failing command that was already superseded in
        # the queue never reaches the device at all.
        app = _app(fail_on={7})
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 7}))
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 8}))
        _run(app.execute_command())
        assert _typed(app.event_bus, EventType.ActuationFault) == []
        assert app.actuator.executed == [{"target_id": 8}]

    def test_non_command_events_are_not_enqueued(self):
        app = _app()
        app._enqueue_command(Event(EventType.ActuationState, {"heartbeat": True}))
        assert app.command_queue.empty()

    def test_empty_queue_is_noop(self):
        app = _app()
        _run(app.execute_command())
        assert app.event_bus.events == []


class TestBacklogDrainsToLatest:
    def test_backlog_executes_only_newest(self):
        app = _app()
        for tid in (1, 2, 3):
            app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": tid}))
        _run(app.execute_command())
        # only the newest intent reached the device
        assert app.actuator.executed == [{"target_id": 3}]
        acks = _typed(app.event_bus, EventType.ActuationAck)
        by_status = {}
        for a in acks:
            by_status.setdefault(a.data["status"], []).append(a.data["target_id"])
        assert by_status["superseded"] == [1, 2]
        assert by_status["ok"] == [3]

    def test_single_command_has_no_superseded_acks(self):
        app = _app()
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 9}))
        _run(app.execute_command())
        acks = _typed(app.event_bus, EventType.ActuationAck)
        assert [a.data["status"] for a in acks] == ["ok"]


class TestSeqEcho:
    def test_ok_ack_echoes_seq(self):
        app = _app()
        app._enqueue_command(Event(EventType.ActuationCommand,
                                   {"target_id": 1, "seq": 41}))
        _run(app.execute_command())
        acks = _typed(app.event_bus, EventType.ActuationAck)
        assert acks[0].data["seq"] == 41

    def test_superseded_and_ok_echo_their_own_seqs(self):
        app = _app()
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 1, "seq": 1}))
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 2, "seq": 2}))
        _run(app.execute_command())
        acks = _typed(app.event_bus, EventType.ActuationAck)
        assert [(a.data["status"], a.data["seq"]) for a in acks] == [
            ("superseded", 1), ("ok", 2)]

    def test_fault_echoes_seq(self):
        app = _app(fail_on={7})
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 7, "seq": 9}))
        _run(app.execute_command())
        faults = _typed(app.event_bus, EventType.ActuationFault)
        assert faults[0].data["seq"] == 9


class TestAgeGate:
    def test_disabled_by_default_executes_old_commands(self):
        import time
        app = _app()          # _max_command_age = 0
        app._enqueue_command(Event(EventType.ActuationCommand,
                                   {"target_id": 1, "seq": 1,
                                    "issued_at": time.time() - 3600}))
        _run(app.execute_command())
        assert app.actuator.executed and app.actuator.executed[0]["target_id"] == 1

    def test_enabled_drops_stale_without_touching_device(self):
        import time
        app = _app()
        app._max_command_age = 0.5
        app._enqueue_command(Event(EventType.ActuationCommand,
                                   {"target_id": 1, "seq": 7,
                                    "issued_at": time.time() - 10}))
        _run(app.execute_command())
        assert app.actuator.executed == []
        acks = _typed(app.event_bus, EventType.ActuationAck)
        assert acks[0].data["status"] == "stale" and acks[0].data["seq"] == 7

    def test_fresh_command_passes_enabled_gate(self):
        import time
        app = _app()
        app._max_command_age = 5.0
        app._enqueue_command(Event(EventType.ActuationCommand,
                                   {"target_id": 2, "seq": 8,
                                    "issued_at": time.time()}))
        _run(app.execute_command())
        assert app.actuator.executed[0]["target_id"] == 2

    def test_command_without_issued_at_is_never_gated(self):
        app = _app()
        app._max_command_age = 0.5
        app._enqueue_command(Event(EventType.ActuationCommand,
                                   {"target_id": 3, "seq": 9}))
        _run(app.execute_command())
        assert app.actuator.executed[0]["target_id"] == 3


class TestSlowPlayDoesNotStarveHeartbeat:
    def test_heartbeat_beats_during_slow_play(self):
        """P1-a: play() runs off the event loop, so a slow device action must
        not block the heartbeat (which would trip a false LINK_LOST)."""
        import time as _time

        app = _app()
        app._heartbeat_interval = 0.02

        def slow_execute(command):
            _time.sleep(0.15)
            app.actuator.executed.append(dict(command))
        app.actuator.execute = slow_execute

        async def scenario():
            hb = asyncio.create_task(app._heartbeat())
            app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 1}))
            await app.execute_command()          # blocks ~0.15s in a thread
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass

        _run(scenario())
        assert app.actuator.executed == [{"target_id": 1}]
        states = _typed(app.event_bus, EventType.ActuationState)
        # With play off the loop, ~0.15s / 0.02s ≈ 7 beats; ≥3 is a safe floor.
        assert len(states) >= 3


class TestHeartbeat:
    def test_heartbeat_publishes_state(self):
        app = _app()

        async def scenario():
            task = asyncio.create_task(app._heartbeat())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(scenario())
        states = _typed(app.event_bus, EventType.ActuationState)
        assert len(states) >= 2
        assert all(s.data == {"heartbeat": True} for s in states)


class TestSetupFailure:
    def test_setup_failure_emits_fault_and_exits(self, monkeypatch):
        app = _app()

        async def _bad_start():
            raise RuntimeError("no effector configured")

        stopped = []

        async def _stop():
            stopped.append(True)

        monkeypatch.setattr(app, "start", _bad_start)
        monkeypatch.setattr(app, "stop", _stop)

        async def fast_sleep(_):
            return None
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        _run(app.serve())
        faults = _typed(app.event_bus, EventType.ActuationFault)
        assert len(faults) == 1 and faults[0].data["code"] == "SETUP_FAILED"
        assert "no effector configured" in faults[0].data["message"]
        assert stopped == [True]
