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
from omnibci.application.events import CommandRequest, CommandResult
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


class _FakeTask:
    """Stand-in for the asyncio render task; done()=False means window open."""
    def __init__(self, done: bool = False):
        self._done = done
    def done(self) -> bool:
        return self._done


def _make(stimulator, render_done: bool = False) -> Stimulation:
    s = Stimulation.__new__(Stimulation)
    s.stim_command_queue = Queue()
    s.result_command_queue = Queue()
    s.stimulator = stimulator
    s.event_bus = _FakeEventBus()
    s._render_task = _FakeTask(done=render_done)  # window open by default
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


def test_ack_error_when_window_closed():
    """Closing the render window keeps the connection but can't display:
    a trial must be acked as error, not silently dropped/hung."""
    async def scenario():
        s = _make(_FakeStimulator(), render_done=True)  # window closed
        s.stim_command_queue.put(Event(EventType.StartStimTrial, data=2))
        await s.execute_command()
        assert s.stimulator.presented == []            # nothing presented
        ack = s.event_bus.published[0]
        assert ack.data["status"] == "error"
        assert "window closed" in ack.data["message"]
    asyncio.run(scenario())


def test_correlated_result_is_emitted_only_after_trial_enters_renderer():
    async def scenario():
        s = _make(_FakeStimulator())
        request = CommandRequest(
            command_id="cmd-1",
            payload={"n_trials": 1, "trial_params": [7]},
        )
        s._put_stim_command_queue(Event(EventType.StartStimTrial, request))
        assert s.event_bus.published == []

        await s.execute_command()

        assert s.stimulator.presented == [7]
        result_events = [
            event for event in s.event_bus.published
            if event.type == EventType.CommandResult
        ]
        assert len(result_events) == 1
        assert result_events[0].data == CommandResult(
            command_id="cmd-1",
            command="StartStimTrial",
            ok=True,
            message="Stimulation trial started",
            error_code=None,
        )
    asyncio.run(scenario())


def test_trial_sequence_preserves_order_and_repetitions_while_waiting_for_renderer():
    class BusyStimulator(_FakeStimulator):
        is_presenting = False

        def present_trial(self, event_id):
            super().present_trial(event_id)
            self.is_presenting = True

    async def scenario():
        s = _make(BusyStimulator())
        sequence = [1, 3, 5, 1, 3, 5]
        request = CommandRequest(
            command_id="sequence",
            payload={"n_trials": len(sequence), "trial_params": sequence},
        )
        s._put_stim_command_queue(Event(EventType.StartStimTrial, request))
        assert s.event_bus.published == []

        for index in range(len(sequence)):
            s.stimulator.is_presenting = False
            await s.execute_command()
            assert s.stimulator.presented == sequence[:index + 1]

            await s.execute_command()
            await s.execute_command()
            assert s.stimulator.presented == sequence[:index + 1]
            assert s.stim_command_queue.qsize() == len(sequence) - index - 1

        results = [
            event.data for event in s.event_bus.published
            if event.type == EventType.CommandResult
        ]
        assert len(results) == 1
        assert results[0].command_id == "sequence"
        assert results[0].ok is True
        assert [
            event.data["target_index"] for event in s.event_bus.published
            if event.type == EventType.StimulationAck
        ] == sequence

    asyncio.run(scenario())


def test_correlated_result_rejects_closed_window():
    async def scenario():
        s = _make(_FakeStimulator(), render_done=True)
        request = CommandRequest(
            command_id="cmd-2",
            payload={"n_trials": 1, "trial_params": [2]},
        )
        s._put_stim_command_queue(Event(EventType.StartStimTrial, request))

        await s.execute_command()

        result = next(
            event.data for event in s.event_bus.published
            if event.type == EventType.CommandResult
        )
        assert result.ok is False
        assert result.error_code == "STIMULATION_WINDOW_CLOSED"
    asyncio.run(scenario())


def test_correlated_result_rejects_invalid_trial_spec_without_queueing():
    s = _make(_FakeStimulator())
    request = CommandRequest(
        command_id="cmd-3",
        payload={"n_trials": 2, "trial_params": [1]},
    )

    s._put_stim_command_queue(Event(EventType.StartStimTrial, request))

    assert s.stim_command_queue.empty()
    result = s.event_bus.published[0]
    assert result.type == EventType.CommandResult
    assert result.data.ok is False
    assert result.data.error_code == "INVALID_STIMULATION_TRIAL"
