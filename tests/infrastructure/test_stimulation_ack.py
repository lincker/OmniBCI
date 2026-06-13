"""
Stimulation-side ack: after the renderer presents a trial, the stimulation
process sends a StimulationAck back to the engine (ok / error).

The Stimulation app is built via __new__ with fakes injected — its real
__init__ constructs an OpenGL Stimulator, which must not run in tests.
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


class _FakeClientMgmt:
    def __init__(self):
        self.sent = []

    async def send_event(self, event):
        self.sent.append(event)
        return True


def _make(stimulator) -> Stimulation:
    s = Stimulation.__new__(Stimulation)
    s.stim_command_queue = Queue()
    s.result_command_queue = Queue()
    s.stimulator = stimulator
    s.client_message_management = _FakeClientMgmt()
    return s


def test_ack_ok_after_successful_present():
    async def scenario():
        s = _make(_FakeStimulator())
        s.stim_command_queue.put(Event(EventType.StartStimTrial, data=3))
        await s.execute_command()
        assert s.stimulator.presented == [3]
        assert len(s.client_message_management.sent) == 1
        ack = s.client_message_management.sent[0]
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
        assert len(s.client_message_management.sent) == 1
        ack = s.client_message_management.sent[0]
        assert ack.data["status"] == "error"
        assert "renderer busy" in ack.data["message"]
    asyncio.run(scenario())


def test_no_ack_when_no_command():
    async def scenario():
        s = _make(_FakeStimulator())
        await s.execute_command()  # empty queues
        assert s.client_message_management.sent == []
    asyncio.run(scenario())
