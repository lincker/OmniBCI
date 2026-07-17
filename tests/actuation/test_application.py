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
        app = _app(fail_on={7})
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 7}))
        app._enqueue_command(Event(EventType.ActuationCommand, {"target_id": 8}))
        _run(app.execute_command())
        _run(app.execute_command())
        faults = _typed(app.event_bus, EventType.ActuationFault)
        assert len(faults) == 1
        assert faults[0].data["code"] == "COMMAND_FAILED" and faults[0].data["target_id"] == 7
        # the next command still executed and acked
        assert app.actuator.executed == [{"target_id": 8}]
        assert len(_typed(app.event_bus, EventType.ActuationAck)) == 1

    def test_non_command_events_are_not_enqueued(self):
        app = _app()
        app._enqueue_command(Event(EventType.ActuationState, {"heartbeat": True}))
        assert app.command_queue.empty()

    def test_empty_queue_is_noop(self):
        app = _app()
        _run(app.execute_command())
        assert app.event_bus.events == []


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
