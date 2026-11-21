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
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.infrastructure.stimulation.stimulator import Stimulator
from omnibci.application.events import CommandRequest, CommandResult
from omnibci.shared.events import Event, EventType


class _FakeStimulator:
    is_presenting = False

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.presented = []
        self.results = []

    def present_trial(self, event_id):
        if self.fail:
            raise RuntimeError("renderer busy")
        self.presented.append(event_id)

    def present_result(self, result):
        self.results.append(result)


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
    s.should_stop = False
    s.stim_command_queue = Queue()
    s.result_command_queue = Queue()
    s.stimulator = stimulator
    s.event_bus = _FakeEventBus()
    s.peer_channel = Mock(send=AsyncMock(return_value=True))
    s._render_task = _FakeTask(done=render_done)  # window open by default
    return s


def test_ack_ok_after_successful_present():
    async def scenario():
        s = _make(_FakeStimulator())
        s._put_stim_command_queue(Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"event_id": 3}]}))
        await s.execute_command()
        assert s.stimulator.presented == [{"event_id": 3}]
        assert len(s.event_bus.published) == 1
        ack = s.event_bus.published[0]
        assert ack.type == EventType.StimulationAck
        assert ack.data["command"] == "StartStimTrial"
        assert ack.data["status"] == "ok"
        assert ack.data["trial_params"] == {"event_id": 3}
    asyncio.run(scenario())


def test_ack_error_when_present_fails():
    async def scenario():
        s = _make(_FakeStimulator(fail=True))
        s._put_stim_command_queue(Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"event_id": 5}]}))
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
        s._put_stim_command_queue(Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"event_id": 2}]}))
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
            payload={"n_trials": 1, "trial_params": [{"event_id": 7}]},
        )
        s._put_stim_command_queue(Event(EventType.StartStimTrial, request))
        assert s.event_bus.published == []

        await s.execute_command()

        assert s.stimulator.presented == [{"event_id": 7}]
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
        sequence = [{"event_id": value} for value in [1, 3, 5, 1, 3, 5]]
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
            event.data["trial_params"] for event in s.event_bus.published
            if event.type == EventType.StimulationAck
        ] == sequence

    asyncio.run(scenario())


def test_correlated_result_rejects_closed_window():
    async def scenario():
        s = _make(_FakeStimulator(), render_done=True)
        request = CommandRequest(
            command_id="cmd-2",
            payload={"n_trials": 1, "trial_params": [{"event_id": 2}]},
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
        payload={"n_trials": 2, "trial_params": [{"event_id": 1}]},
    )

    s._put_stim_command_queue(Event(EventType.StartStimTrial, request))

    assert s.stim_command_queue.empty()
    result = s.event_bus.published[0]
    assert result.type == EventType.CommandResult
    assert result.data.ok is False
    assert result.data.error_code == "INVALID_STIMULATION_TRIAL"


@pytest.mark.parametrize("render_missing", [False, True])
@pytest.mark.parametrize("readiness", [True, RuntimeError("must not query closed paradigm")])
def test_closed_renderer_rejects_trial_without_querying_readiness(render_missing, readiness):
    async def scenario():
        stimulator = Stimulator(settings_snapshot={})
        stimulator.paradigm = Mock()
        if isinstance(readiness, Exception):
            stimulator.paradigm.is_presenting.side_effect = readiness
        else:
            stimulator.paradigm.is_presenting.return_value = readiness
        s = _make(stimulator, render_done=True)
        if render_missing:
            s._render_task = None
        s._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "closed", {"n_trials": 1, "trial_params": [{"event_id": 4}]},
        )))
        s.result_command_queue.put(Event(EventType.TrialCompleted, {"target_id": 3}))

        await s.execute_command()

        stimulator.paradigm.is_presenting.assert_not_called()
        stimulator.paradigm.present_trial.assert_not_called()
        stimulator.paradigm.present_result.assert_not_called()
        assert s.stim_command_queue.empty()
        result = next(event.data for event in s.event_bus.published
                      if event.type == EventType.CommandResult)
        assert not result.ok
        assert result.error_code == "STIMULATION_WINDOW_CLOSED"

    asyncio.run(scenario())


def test_busy_paradigm_still_receives_result_feedback():
    async def scenario():
        stimulator = _FakeStimulator()
        stimulator.is_presenting = True
        s = _make(stimulator)
        s._put_stim_command_queue(Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"event_id": 4}]}))
        s.result_command_queue.put(Event(EventType.TrialCompleted, {"target_id": 3}))

        await s.execute_command()

        assert stimulator.presented == []
        assert stimulator.results == [{"target_id": 3}]
        assert s.stim_command_queue.qsize() == 1
        assert s.event_bus.published == []

    asyncio.run(scenario())


@pytest.mark.parametrize("readiness", [RuntimeError("readiness failed"), None, 0, 1, "busy"])
def test_readiness_failure_rejects_pending_trials_and_stops_admission(readiness):
    async def scenario():
        stimulator = Stimulator(settings_snapshot={})
        stimulator.paradigm = Mock()
        if isinstance(readiness, Exception):
            stimulator.paradigm.is_presenting.side_effect = readiness
        else:
            stimulator.paradigm.is_presenting.return_value = readiness
        s = _make(stimulator)
        for command_id, sequence in [("batch", [1, 2, 3]), ("next", [4])]:
            s._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
                command_id, {"n_trials": len(sequence), "trial_params": [{"event_id": value} for value in sequence]},
            )))
        s.result_command_queue.put(Event(EventType.TrialCompleted, {"target_id": 3}))

        with pytest.raises(RuntimeError if isinstance(readiness, Exception) else TypeError) as raised:
            await s.execute_command()

        if isinstance(readiness, Exception):
            assert raised.value is readiness
        assert s.should_stop
        assert s.stim_command_queue.empty()
        stimulator.paradigm.present_trial.assert_not_called()
        stimulator.paradigm.present_result.assert_not_called()
        sent = [call.args[0] for call in s.peer_channel.send.await_args_list]
        results = [event.data for event in sent if event.type == EventType.CommandResult]
        assert [result.command_id for result in results] == ["batch", "next"]
        assert all(not result.ok and result.error_code == "STIMULATION_READINESS_FAILED"
                   for result in results)
        acks = [event.data for event in sent if event.type == EventType.StimulationAck]
        assert [ack["trial_params"] for ack in acks] == [{"event_id": value} for value in [1, 2, 3, 4]]
        assert all(ack["status"] == "error" for ack in acks)
        assert s.event_bus.published == []

        # A transiently recovered query cannot restart a faulted instance.
        stimulator.paradigm.is_presenting.side_effect = None
        stimulator.paradigm.is_presenting.return_value = False
        s._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "late", {"n_trials": 1, "trial_params": [{"event_id": 5}]},
        )))
        await s.execute_command()
        assert s.stim_command_queue.empty()
        stimulator.paradigm.is_presenting.assert_called_once_with()
        stimulator.paradigm.present_trial.assert_not_called()
        stimulator.paradigm.present_result.assert_not_called()
        result = s.event_bus.published[-1].data
        assert not result.ok
        assert result.error_code == "STIMULATION_STOPPING"

    asyncio.run(scenario())


def test_missing_host_readiness_is_not_treated_as_idle():
    async def scenario():
        s = _make(object())
        s._put_stim_command_queue(Event(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"event_id": 2}]}))

        with pytest.raises(AttributeError, match="is_presenting"):
            await s.execute_command()

        assert s.should_stop
        ack = s.peer_channel.send.await_args.args[0].data
        assert ack["status"] == "error"

    asyncio.run(scenario())


@pytest.mark.parametrize("delivery", [False, OSError("engine connection lost")])
def test_failed_fault_receipt_does_not_hide_readiness_error(delivery):
    async def scenario():
        error = RuntimeError("readiness failed")
        stimulator = Stimulator(settings_snapshot={})
        stimulator.paradigm = Mock()
        stimulator.paradigm.is_presenting.side_effect = error
        s = _make(stimulator)
        if isinstance(delivery, Exception):
            s.peer_channel.send.side_effect = delivery
        else:
            s.peer_channel.send.return_value = delivery
        for command_id in ("first", "second"):
            s._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
                command_id, {"n_trials": 1, "trial_params": [{"event_id": 0}]},
            )))

        with pytest.raises(RuntimeError) as raised:
            await s.execute_command()

        assert raised.value is error
        assert any("deliver" in note for note in error.__notes__)
        assert s.should_stop
        assert s.stim_command_queue.empty()
        assert s.peer_channel.send.await_count == 4

    asyncio.run(scenario())


def test_readiness_fault_flushes_receipts_before_existing_shutdown():
    async def scenario():
        lifecycle = []
        released = asyncio.Event()
        stimulator = Stimulator(settings_snapshot={})
        error = RuntimeError("readiness failed")
        stimulator.paradigm = Mock()
        stimulator.paradigm.is_presenting.side_effect = error

        async def render():
            await released.wait()

        def stop_render():
            lifecycle.append("renderer-stop")
            released.set()

        stimulator.start_async = render
        stimulator.request_stop = stop_render
        s = _make(stimulator)
        s.start = AsyncMock()
        s.stop = AsyncMock(side_effect=lambda: lifecycle.append("ipc-stop"))
        s._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "pending", {"n_trials": 1, "trial_params": [{"event_id": 2}]},
        )))

        async def send(event):
            assert s.should_stop
            assert not released.is_set()
            await asyncio.sleep(0)
            s._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
                "arrived-during-fault", {"n_trials": 1, "trial_params": [{"event_id": 0}]},
            )))
            s._put_sys_command_queue(Event(EventType.TrialCompleted, {"target_id": 3}))
            assert s.stim_command_queue.empty()
            assert s.result_command_queue.empty()
            lifecycle.append(event.type.name)
            return True

        s.peer_channel.send.side_effect = send
        with pytest.raises(RuntimeError) as raised:
            await asyncio.wait_for(s.serve(), timeout=0.5)

        assert raised.value is error
        assert lifecycle == ["CommandResult", "StimulationAck", "renderer-stop", "ipc-stop"]
        assert s._render_task.done()
        stimulator.paradigm.is_presenting.assert_called_once_with()
        stimulator.paradigm.present_trial.assert_not_called()
        stimulator.paradigm.present_result.assert_not_called()

    asyncio.run(scenario())
