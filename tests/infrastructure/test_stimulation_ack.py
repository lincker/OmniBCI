"""
Stimulation-side ack: after the renderer presents a trial, the stimulation
process sends a StimulationAck back to the engine (ok / error).

The Stimulation app is built via __new__ with fakes injected — its real
__init__ constructs an OpenGL Stimulator and a PeerChannel, which must not
run in tests.

Acks are now published onto the EventBus (PeerChannel's outbound subscription
picks them up); the test captures them via a fake EventBus.
"""
from __future__ import annotations

import asyncio
from queue import Queue

from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.shared.events import Event, EventType


class _FakeStimulator:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.presented = []

    def present_trial(self, event_id):
        if self.fail:
            raise RuntimeError("renderer busy")
        self.presented.append(event_id)


class _FakeEventBus:
    """Minimal EventBus stub that records put_event calls."""
    def __init__(self):
        self.published: list[Event] = []

    def put_event(self, event: Event) -> None:
        self.published.append(event)


def _make(stimulator) -> Stimulation:
    s = Stimulation.__new__(Stimulation)
    s.stim_command_queue = Queue()
    s.result_command_queue = Queue()
    s.stimulator = stimulator
    s.event_bus = _FakeEventBus()
    return s


def test_ack_ok_after_successful_present():
    async def scenario():
        s = _make(_FakeStimulator())
        s.stim_command_queue.put(Event(EventType.StartStimTrial, data=3))
        await s.execute_command()
        assert s.stimulator.presented == [3]
        assert len(s.event_bus.published) == 1
        ack = s.event_bus.published[0]
        assert ack.type == EventType.StimulationAck
        assert ack.data["command"] == "StartStimTrial"
        assert ack.data["status"] == "ok"
        assert ack.data["target_index"] == 3
    asyncio.run(scenario())


def test_ack_error_when_present_fails():
    async def scenario():
        s = _make(_FakeStimulator(fail=True))
        s.stim_command_queue.put(Event(EventType.StartStimTrial, data=5))
        await s.execute_command()
        assert len(s.event_bus.published) == 1
        ack = s.event_bus.published[0]
        assert ack.data["status"] == "error"
        assert "renderer busy" in ack.data["message"]
    asyncio.run(scenario())


def test_no_ack_when_no_command():
    async def scenario():
        s = _make(_FakeStimulator())
        await s.execute_command()  # empty queues
        assert s.event_bus.published == []
    asyncio.run(scenario())
