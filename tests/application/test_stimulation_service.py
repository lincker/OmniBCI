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
        engine_service.dispatch = MagicMock(side_effect=RuntimeError("not running"))

        with pytest.raises(RuntimeError, match="not running"):
            stimulation_service.handle_start_stimulation(
                StartStimulationCommand(paradigm="classic40SSVEP")
            )

        manager.start.assert_called_once_with(paradigm="classic40SSVEP")
        manager.stop.assert_called_once_with()
        assert stimulation_service.is_active is False

    def test_stopped_engine_rejects_before_spawning_a_process(
        self, stimulation_service, engine_service,
    ):
        manager = MagicMock()
        stimulation_service._process_manager = manager
        engine_service._running = False
        with pytest.raises(RuntimeError, match="Start the engine"):
            stimulation_service.handle_start_stimulation(StartStimulationCommand())
        manager.start.assert_not_called()
        manager.stop.assert_not_called()

    def test_stimulation_does_not_require_acquisition_or_processing(
        self, stimulation_service, mock_engine,
    ):
        mock_engine.acquisition_worker.is_alive.return_value = False
        mock_engine.signal_processing_worker.is_alive.return_value = False
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        assert stimulation_service.is_active is True

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

    def test_external_stimulation_requires_enable_again_after_foundation_restart(
        self, stimulation_service, engine_service,
    ):
        manager = MagicMock(auto_spawn=False, is_alive=False)
        stimulation_service._process_manager = manager
        engine_service._applied_snapshot = {"CONFIG.generation": "first", "STIMULATION.auto_spawn": False}
        engine_service.is_peer_connected = MagicMock(return_value=True)
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        assert stimulation_service.is_active
        engine_service._running = False
        assert not stimulation_service.is_active
        engine_service._applied_snapshot = {"CONFIG.generation": "second", "STIMULATION.auto_spawn": False}
        engine_service._running = True
        assert not stimulation_service.is_active
        with pytest.raises(RuntimeError, match="Start stimulation"):
            stimulation_service.handle_start_trial(StartStimTrialCommand())
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        assert stimulation_service.is_active

    def test_applied_external_mode_does_not_require_a_managed_process_to_be_alive(
        self, stimulation_service, engine_service,
    ):
        stimulation_service._process_manager = MagicMock(auto_spawn=True, is_alive=False)
        engine_service._applied_snapshot = {"STIMULATION.auto_spawn": False}
        stimulation_service._active_identity = (None, None)
        assert stimulation_service.is_active

    def test_external_reconfiguration_requires_enable_but_other_modules_do_not(
        self, stimulation_service, engine_service,
    ):
        stimulation_service._process_manager = MagicMock(auto_spawn=False, is_alive=False)
        engine_service.is_peer_connected = MagicMock(return_value=True)
        engine_service._applied_snapshot = {"CONFIG.generation": "foundation", "STIMULATION.auto_spawn": False,
                                            "CONFIG.module_revisions": {"stimulation": "first"}}
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        assert stimulation_service.is_active
        engine_service.is_peer_connected.return_value = False
        assert not stimulation_service.is_active
        engine_service._applied_snapshot["CONFIG.module_revisions"]["stimulation"] = "second"
        engine_service.is_peer_connected.return_value = True
        assert not stimulation_service.is_active
        with pytest.raises(RuntimeError, match="Start stimulation"):
            stimulation_service.handle_start_trial(StartStimTrialCommand())
        stimulation_service.handle_start_stimulation(StartStimulationCommand())
        assert stimulation_service.is_active
        engine_service._applied_snapshot["CONFIG.module_revisions"].update(acquisition="changed", processing="changed")
        assert stimulation_service.is_active


class TestStimulationStop:
    def test_stop_dispatches_event(self, stimulation_service, mock_engine):
        stimulation_service._active_identity = (None, None)
        stimulation_service.handle_stop_stimulation(StopStimulationCommand())
        event = mock_engine.event_bus.put_event.call_args[0][0]
        assert event.type == EventType.StopStimulation

    def test_stop_clears_active_flag(self, stimulation_service):
        stimulation_service._active_identity = (None, None)
        stimulation_service.handle_stop_stimulation(StopStimulationCommand())
        assert stimulation_service.is_active is False

    def test_dispatch_failure_still_stops_process_and_preserves_error(
        self, stimulation_service, engine_service
    ):
        manager = MagicMock()
        manager.stop.side_effect = RuntimeError("cleanup also failed")
        stimulation_service._process_manager = manager
        stimulation_service._active_identity = (None, None)
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
        stimulation_service._active_identity = (None, None)

        with pytest.raises(CommandTimeoutError):
            stimulation_service.handle_stop_stimulation(StopStimulationCommand())

        manager.stop.assert_not_called()
        assert stimulation_service.is_active is True

    def test_applied_external_mode_requires_a_receipt_before_first_enable(
        self, stimulation_service, engine_service, mock_engine,
    ):
        manager = MagicMock(auto_spawn=True)
        broker = MagicMock()
        broker.request.side_effect = CommandTimeoutError("stop", "StopStimulation", 0.1)
        stimulation_service._process_manager = manager
        stimulation_service._command_results = broker
        engine_service._applied_snapshot = {"STIMULATION.auto_spawn": False}
        with pytest.raises(CommandTimeoutError):
            stimulation_service.handle_stop_stimulation(StopStimulationCommand())
        broker.request.assert_called_once_with(EventType.StopStimulation)
        manager.stop.assert_not_called()
        mock_engine.event_bus.put_event.assert_not_called()


class TestStimulationTrial:
    @pytest.fixture(autouse=True)
    def enabled(self, stimulation_service):
        stimulation_service.handle_start_stimulation(StartStimulationCommand())

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


def test_connected_external_node_cannot_run_trials_before_enable(stimulation_service, engine_service, mock_engine):
    engine_service.is_peer_connected = MagicMock(return_value=True)
    broker = MagicMock()
    stimulation_service._command_results = broker
    with pytest.raises(RuntimeError, match="Start stimulation"):
        stimulation_service.handle_start_trial(StartStimTrialCommand())
    broker.request.assert_not_called()
    mock_engine.event_bus.put_event.assert_not_called()
