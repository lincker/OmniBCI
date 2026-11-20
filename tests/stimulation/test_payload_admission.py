"""Stimulation IPC admits explicit snapshots and projects only result payloads."""
import asyncio

import pytest

from omnibci.application.events import CommandRequest
from omnibci.shared.events import Event, EventType
from tests.infrastructure.test_stimulation_ack import _FakeStimulator, _make


@pytest.mark.parametrize('payload', [
    {'n_trials': 1}, {'n_trials': True, 'trial_params': [{}]},
    {'n_trials': '1', 'trial_params': [{}]}, {'n_trials': 1, 'trial_params': [3]},
    {'n_trials': 1, 'trial_params': [None]}, {'n_trials': 1, 'trial_params': [{'x': float('nan')}]},
    {'n_trials': 1, 'trial_params': [{'x': 'x' * (1024 * 1024)}]},
])
def test_ipc_rejects_implicit_or_invalid_trials_before_queueing(payload):
    app = _make(_FakeStimulator())
    app._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest('invalid', payload)))
    assert app.stim_command_queue.empty()
    result, = app.event_bus.published
    assert result.type == EventType.CommandResult
    assert result.data.command_id == 'invalid'
    assert not result.data.ok and result.data.error_code == 'INVALID_STIMULATION_TRIAL'


def test_queued_objects_and_receipts_do_not_borrow_request_data():
    async def scenario():
        app = _make(_FakeStimulator())
        params = [{'flashes': [3, 1]}]
        app._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            'valid', {'n_trials': 1, 'trial_params': params},
        )))
        params[0]['flashes'].append(7)
        await app.execute_command()
        assert app.stimulator.presented == [{'flashes': [3, 1]}]
        ack = next(event.data for event in app.event_bus.published if event.type == EventType.StimulationAck)
        assert ack['trial_params'] == {'flashes': [3, 1]}
        assert 'target_index' not in ack
    asyncio.run(scenario())


def test_feedback_projects_only_the_result_object():
    async def scenario():
        app = _make(_FakeStimulator())
        app.result_command_queue.put(Event(EventType.TrialCompleted, {
            'target_id': 3, 'generation': 'private', 'processing_revision': 'private', 'event_id': 2,
        }))
        await app.execute_command()
        assert app.stimulator.results == [{'target_id': 3}]
    asyncio.run(scenario())


@pytest.mark.parametrize('value', [3, '3', True, {'target_id': '3'}, {'target_id': True},
                                  {'target_id': 3.5}, {'target_id': -1}, {}])
def test_feedback_does_not_coerce_malformed_targets(value):
    async def scenario():
        app = _make(_FakeStimulator())
        app.result_command_queue.put(Event(EventType.TrialCompleted, value))
        await app.execute_command()
        assert app.stimulator.results == []
    asyncio.run(scenario())
