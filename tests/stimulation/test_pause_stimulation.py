"""A runtime pause acknowledges real renderer exit and trigger clearing."""
import asyncio
from queue import Queue
from unittest.mock import Mock

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.shared.events import Event, EventType


def app():
    instance = Stimulation.__new__(Stimulation)
    instance.stim_command_queue = Queue()
    instance.result_command_queue = Queue()
    instance.event_bus = Mock()
    instance.stimulator = Mock()
    instance._runtime_snapshot = {"CONFIG.revision": "revision", "CONFIG.generation": "generation"}
    instance._paused = False
    return instance


def receipts(instance, command):
    return [call.args[0].data for call in instance.event_bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult
            and call.args[0].data.command == command.name]


def test_pause_joins_renderer_clears_trigger_and_cancels_future_trials():
    async def scenario():
        instance = app()
        exited = asyncio.Event()
        instance._render_task = asyncio.create_task(exited.wait())
        instance._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "queued-trials", {"n_trials": 2},
        )))
        pause = asyncio.create_task(instance._pause_stimulation(Event(
            EventType.PauseStimulation, CommandRequest("pause"),
        )))
        await asyncio.sleep(0)
        assert not pause.done()
        assert receipts(instance, EventType.PauseStimulation) == []
        instance.stimulator.stop.assert_called_once()
        instance.stimulator.clear_trigger.assert_not_called()
        assert instance.stim_command_queue.empty()
        assert receipts(instance, EventType.StartStimTrial)[0].ok is False
        await instance.execute_command()
        instance.stimulator.present_trial.assert_not_called()
        exited.set()
        await pause
        instance.stimulator.clear_trigger.assert_called_once()
        result = receipts(instance, EventType.PauseStimulation)[0]
        assert result.command_id == "pause"
        assert result.ok
        assert result.data == {
            "paused": True, "renderer_stopped": True, "cancelled_trials": 2,
            "generation": "generation", "config_revision": "revision",
        }
        instance._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "late-trial", {"n_trials": 1},
        )))
        assert instance.stim_command_queue.empty()
        assert receipts(instance, EventType.StartStimTrial)[-1].ok is False

    asyncio.run(scenario())


def test_trigger_clear_failure_leaves_paused_without_success_receipt():
    async def scenario():
        instance = app()
        instance._render_task = asyncio.create_task(asyncio.sleep(0))
        instance.stimulator.clear_trigger.side_effect = OSError("trigger clear failed")
        await instance._pause_stimulation(Event(EventType.PauseStimulation, CommandRequest("pause")))
        result = receipts(instance, EventType.PauseStimulation)[0]
        assert not result.ok
        assert "trigger clear failed" in result.message
        assert result.data is None
        assert instance._paused

    asyncio.run(scenario())


def test_renderer_failure_is_not_a_successful_pause():
    async def scenario():
        instance = app()

        async def failed_renderer():
            raise RuntimeError("renderer teardown failed")

        instance._render_task = asyncio.create_task(failed_renderer())
        await instance._pause_stimulation(Event(EventType.PauseStimulation, CommandRequest("pause")))
        result = receipts(instance, EventType.PauseStimulation)[0]
        assert not result.ok
        assert "renderer teardown failed" in result.message
        assert instance._paused
        instance.stimulator.clear_trigger.assert_called_once()

    asyncio.run(scenario())
