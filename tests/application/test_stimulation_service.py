"""
Unit tests for StimulationAppService.
"""
from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.shared.events import EventType


class TestStimulationStart:
    def test_start_dispatches_event(self, stimulation_service, mock_engine):
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StartStimulation

    def test_start_sets_active_flag(self, stimulation_service):
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        assert stimulation_service.is_active is True

    def test_start_with_paradigm_name(self, stimulation_service, mock_engine):
        stimulation_service.handle_start_stimulation(
            StartStimulationCommand(paradigm="classic40SSVEP")
        )
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.data == "classic40SSVEP"


class TestStimulationStop:
    def test_stop_dispatches_event(self, stimulation_service, mock_engine):
        stimulation_service._active = True
        stimulation_service.handle_stop_stimulation(StopStimulationCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StopStimulation

    def test_stop_clears_active_flag(self, stimulation_service):
        stimulation_service._active = True
        stimulation_service.handle_stop_stimulation(StopStimulationCommand())
        assert stimulation_service.is_active is False


class TestStimulationTrial:
    def test_trial_dispatches_event(self, stimulation_service, mock_engine):
        stimulation_service.handle_start_trial(StartStimTrialCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StartStimTrial

    def test_trial_with_params(self, stimulation_service, mock_engine):
        stimulation_service.handle_start_trial(
            StartStimTrialCommand(n_trials=1, trial_params=[5]))
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.data == {"n_trials": 1, "trial_params": [5]}
