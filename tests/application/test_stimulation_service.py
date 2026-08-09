"""
Unit tests for StimulationAppService.
"""
from unittest.mock import MagicMock

import pytest

from omnibci.application.commands.stimulation_commands import (
    StartStimulationCommand,
    StopStimulationCommand,
    StartStimTrialCommand,
)
from omnibci.shared.events import EventType
from omnibci.application.services.command_result_service import CommandTimeoutError


@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    """Command-focused tests exercise a runtime-ready EnginePort."""
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


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

    def test_dispatch_failure_rolls_back_spawned_process(
        self, stimulation_service, engine_service
    ):
        manager = MagicMock()
        stimulation_service._process_manager = manager
        engine_service._running = False

        with pytest.raises(RuntimeError, match="not running"):
            stimulation_service.handle_start_stimulation(
                StartStimulationCommand(paradigm="classic40SSVEP")
            )

        manager.start.assert_called_once_with(paradigm="classic40SSVEP")
        manager.stop.assert_called_once_with()
        assert stimulation_service.is_active is False

    def test_child_startup_failure_never_dispatches_or_marks_active(
        self, stimulation_service, mock_engine
    ):
        manager = MagicMock()
        manager.start.side_effect = RuntimeError("renderer unavailable")
        stimulation_service._process_manager = manager

        with pytest.raises(RuntimeError, match="renderer unavailable"):
            stimulation_service.handle_start_stimulation(
                StartStimulationCommand(paradigm="classic40SSVEP")
            )

        mock_engine.event_bus.put_event.assert_not_called()
        manager.stop.assert_not_called()
        assert stimulation_service.is_active is False

    def test_external_process_rejects_unfulfillable_paradigm_change(
        self, stimulation_service, engine_service
    ):
        manager = MagicMock()
        manager.auto_spawn = False
        stimulation_service._process_manager = manager
        engine_service.is_peer_connected = MagicMock(return_value=True)

        with pytest.raises(RuntimeError, match="Cannot change paradigm"):
            stimulation_service.handle_start_stimulation(
                StartStimulationCommand(paradigm="classic40SSVEP")
            )

        manager.stop.assert_called_once_with()
        assert stimulation_service.is_active is False


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

    def test_dispatch_failure_still_stops_process_and_preserves_error(
        self, stimulation_service, engine_service
    ):
        manager = MagicMock()
        manager.stop.side_effect = RuntimeError("cleanup also failed")
        stimulation_service._process_manager = manager
        stimulation_service._active = True
        engine_service._running = False

        with pytest.raises(RuntimeError, match="NeuroEngine is not running"):
            stimulation_service.handle_stop_stimulation(StopStimulationCommand())

        manager.stop.assert_called_once_with()
        assert stimulation_service.is_active is False

    def test_external_stop_timeout_preserves_unknown_active_state(
        self, stimulation_service
    ):
        manager = MagicMock()
        manager.auto_spawn = False
        broker = MagicMock()
        broker.request.side_effect = CommandTimeoutError(
            "stim-stop", "StopStimulation", 0.1
        )
        stimulation_service._process_manager = manager
        stimulation_service._command_results = broker
        stimulation_service._active = True

        with pytest.raises(CommandTimeoutError):
            stimulation_service.handle_stop_stimulation(StopStimulationCommand())

        manager.stop.assert_not_called()
        assert stimulation_service.is_active is True


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

    def test_trial_waits_for_correlated_execution_result(
        self, stimulation_service
    ):
        broker = MagicMock()
        stimulation_service._command_results = broker

        stimulation_service.handle_start_trial(
            StartStimTrialCommand(n_trials=1, trial_params=[5])
        )

        broker.request.assert_called_once_with(
            EventType.StartStimTrial,
            {"n_trials": 1, "trial_params": [5]},
        )
