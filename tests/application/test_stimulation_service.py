"""The renderer belongs to one independent, recoverable Stimulation session."""
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from omnibci.application.commands.stimulation_commands import StartStimulationCommand, StartStimTrialCommand
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.services.command_result_service import CommandDeliveryError, CommandTimeoutError
from omnibci.shared.events import Event, EventType


def start(service, snapshot):
    return service.handle_start_stimulation(StartStimulationCommand(), settings_snapshot=snapshot)


def test_prepare_connect_enable_and_restart_preserve_or_replace_the_correct_identity(
    stimulation_service, stimulation_host, stimulation_snapshot, engine_service,
):
    svc = stimulation_service
    svc.prepare_external()
    first = svc.applied_snapshot
    assert first["CONFIG.engine_revision"] is None
    assert not engine_service.is_engine_ready
    assert start(svc, stimulation_snapshot) == "prepared"
    svc.prepare_external()
    assert svc.applied_snapshot == first
    stimulation_host.is_peer_connected.return_value = True
    assert svc.is_connected and not svc.is_active
    with pytest.raises(RuntimeError, match="Start stimulation"):
        svc.handle_start_trial(StartStimTrialCommand(trial_params=[{}]))
    assert start(svc, stimulation_snapshot) == "started"
    assert start(svc, stimulation_snapshot) == "started"
    assert svc.runtime_package_snapshots == (first,)
    svc._process_manager.start.assert_not_called()
    engine_service.admission.quarantine("Engine recovery", "engine")
    svc.handle_start_trial(StartStimTrialCommand(trial_params=[{"event_id": 7}]))
    assert svc._command_results.requests == [(EventType.StartStimTrial, {"n_trials": 1, "trial_params": [{"event_id": 7}]})]
    stimulation_host.cancel_unaccepted_peer.return_value = False
    svc.stop()
    assert svc._command_results.requests[-1] == (EventType.StopStimulation, None)
    assert svc.owner_identity() == ("stopped", None, None)
    svc.stop()
    svc.prepare_external()
    assert svc.applied_snapshot["CONFIG.generation"] != first["CONFIG.generation"]
    assert svc.applied_snapshot["CONFIG.operation_id"] != first["CONFIG.operation_id"]


def test_listener_only_stop_skips_renderer_and_remains_idempotent(stimulation_service, stimulation_host):
    stimulation_service.prepare_external()
    stimulation_service.stop()
    stimulation_service.stop()
    assert not stimulation_service.runtime_package_snapshots
    assert not stimulation_service._command_results.requests
    stimulation_host.cancel_unaccepted_peer.assert_called_once_with("stimulation")


@pytest.mark.parametrize("command", [EventType.StopStimulation, EventType.StartStimTrial])
@pytest.mark.parametrize("unknown", [True, False])
def test_delivery_failure_preserves_ownership_and_scopes_recovery(
    stimulation_service, stimulation_snapshot, stimulation_host, engine_service, command, unknown,
):
    svc = stimulation_service
    stimulation_host.is_peer_connected.return_value = True
    stimulation_host.cancel_unaccepted_peer.return_value = False
    start(svc, stimulation_snapshot)
    old = svc.applied_snapshot
    svc._command_results.request = MagicMock(side_effect=CommandDeliveryError("request", command.name,
                                                                             "write failed", execution_unknown=unknown))
    with pytest.raises(CommandDeliveryError):
        if command == EventType.StopStimulation:
            svc.stop()
        else:
            svc.request(command, {"n_trials": 1, "trial_params": [{}]})
    assert svc.applied_snapshot == old
    assert engine_service.admission.requires_recovery("stimulation") == (unknown or command == EventType.StopStimulation)
    assert not engine_service.admission.requires_recovery("engine")
    svc._command_results.request.side_effect = None
    svc.stop()
    assert not engine_service.admission.recovery_required


def test_recovery_prepares_retained_listener_and_stop_retries_only_unconfirmed_cleanup(
    stimulation_service, stimulation_host, stimulation_snapshot,
):
    svc = stimulation_service
    stimulation_host.is_peer_connected.return_value = True
    stimulation_host.cancel_unaccepted_peer.return_value = False
    start(svc, stimulation_snapshot)
    retained = svc.applied_snapshot
    svc._command_results.request = MagicMock(side_effect=CommandTimeoutError("lost", "StopStimulation", .01))
    with pytest.raises(CommandTimeoutError):
        svc.stop()
    with pytest.raises(ProfileRevisionConflict):
        start(svc, stimulation_snapshot)
    changed = {**stimulation_snapshot, "CONFIG.module_revisions": {"stimulation": "b" * 64}}
    svc.prepare_external(changed)
    assert svc.applied_snapshot == retained
    svc._command_results.request.side_effect = None
    stimulation_host.close_peer_sync.side_effect = OSError("close failed")
    with pytest.raises(OSError):
        svc.stop()
    stimulation_host.close_peer_sync.side_effect = None
    svc.stop()
    assert svc._command_results.request.call_count == 2


def test_managed_renderer_starts_once_stops_after_engine_failure_and_preserves_uncertain_cleanup(
    stimulation_service, stimulation_host, stimulation_snapshot, engine_service,
):
    svc = stimulation_service
    selected = {**stimulation_snapshot, "STIMULATION.auto_spawn": True}
    start(svc, selected)
    start(svc, selected)
    svc._process_manager.start.assert_called_once_with(settings_snapshot=svc.applied_snapshot)
    assert svc.is_active
    engine_service.admission.quarantine("Engine crashed", "engine")
    stimulation_host.is_peer_connected.return_value = True
    stimulation_host.dispatch.side_effect = OSError("cannot send")
    with pytest.raises(OSError):
        svc.stop()
    svc._process_manager.stop.assert_called_once()
    assert svc.runtime_package_snapshots
    stimulation_host.dispatch.side_effect = None
    svc.stop()
    assert not svc.runtime_package_snapshots


def test_failed_managed_launch_cleans_process_and_listener(stimulation_service, stimulation_host, stimulation_snapshot):
    svc = stimulation_service
    svc._process_manager.start.side_effect = RuntimeError("renderer unavailable")
    with pytest.raises(RuntimeError, match="renderer unavailable"):
        start(svc, {**stimulation_snapshot, "STIMULATION.auto_spawn": True})
    svc._process_manager.stop.assert_called_once()
    stimulation_host.close_peer_sync.assert_called_once()
    assert not svc.is_active and not svc.runtime_package_snapshots


def test_changed_external_configuration_replaces_only_after_confirmed_cleanup(
    stimulation_service, stimulation_host, stimulation_snapshot,
):
    svc = stimulation_service
    svc.prepare_external()
    old = svc.applied_snapshot
    candidate = deepcopy(stimulation_snapshot)
    candidate["CONFIG.module_revisions"]["stimulation"] = "b" * 64
    svc.prepare_external(candidate)
    assert svc.owner_identity()[2] == "b" * 64
    assert svc.owner_identity()[1] != old["CONFIG.generation"]
    stimulation_host.cancel_unaccepted_peer.assert_called_once()


def test_feedback_requires_activation_and_connection(stimulation_service, stimulation_host, stimulation_snapshot):
    svc = stimulation_service
    feedback = Event(EventType.TrialCompleted, {})
    svc.prepare_external()
    stimulation_host.is_peer_connected.return_value = True
    svc.on_trial_completed(feedback)
    stimulation_host.dispatch.assert_not_called()
    start(svc, stimulation_snapshot)
    svc.on_trial_completed(feedback)
    stimulation_host.dispatch.assert_called_once_with(feedback)
    stimulation_host.is_peer_connected.return_value = False
    svc.on_trial_completed(feedback)
    assert stimulation_host.dispatch.call_count == 1


def test_only_current_stimulation_delivery_fault_can_quarantine(stimulation_service, engine_service):
    svc = stimulation_service
    svc._command_results = MagicMock()
    for role, channel in [("actuation", "current"), ("stimulation", "old")]:
        svc.on_peer_delivery_fault(Event(EventType.PeerDeliveryFault, {"peer": role, "channel_id": channel, "reason": "lost"}))
    assert not engine_service.admission.recovery_required
    event = Event(EventType.PeerDeliveryFault, {"peer": "stimulation", "channel_id": "current", "reason": "lost"})
    svc.on_peer_delivery_fault(event)
    assert engine_service.admission.recovery_status == {"stimulation": "lost"}
    svc._command_results.on_peer_delivery_fault.assert_called_once_with(event)


def test_feedback_status_matches_admission_during_recovery(stimulation_service, stimulation_host, stimulation_snapshot, engine_service):
    stimulation_host.is_peer_connected.return_value = True
    start(stimulation_service, stimulation_snapshot)
    assert stimulation_service.status(True)["engine_linked"]
    engine_service.admission.quarantine("Unconfirmed trial", "stimulation")
    assert not stimulation_service.status(True)["engine_linked"]
    stimulation_service.on_trial_completed(Event(EventType.TrialCompleted))
    stimulation_host.dispatch.assert_not_called()


def test_publication_requires_the_current_listener_operation(stimulation_service, stimulation_host):
    from omnibci.application.configuration.errors import ConfigProfileConflictError
    stimulation_service.prepare_external()
    stimulation_host.node_operation_id.side_effect = lambda role: "different-operation"
    with pytest.raises(ConfigProfileConflictError, match="retained"):
        stimulation_service.node_configuration_snapshot(lambda: {})


def test_exited_managed_stimulation_reports_stopped_with_recovery_and_mints_fresh_identity(
    stimulation_service, stimulation_host, stimulation_snapshot, engine_service,
):
    svc = stimulation_service
    selected = {**stimulation_snapshot, "STIMULATION.auto_spawn": True}
    start(svc, selected)
    first = svc.applied_snapshot
    # Simulate an unexpected clean exit (exitcode=0): process dies without stop().
    svc._process_manager.is_alive = False
    assert svc.owner_identity() == ("stopped", None, None)
    assert engine_service.admission.requires_recovery("stimulation")
    assert svc.runtime_package_snapshots == (first,)
    with pytest.raises(ProfileRevisionConflict):
        start(svc, selected)
    # Confirmed clean stop releases the old session and clears recovery.
    svc.stop()
    assert not engine_service.admission.requires_recovery("stimulation")
    assert svc.owner_identity() == ("stopped", None, None)
    start(svc, selected)
    assert svc.applied_snapshot["CONFIG.generation"] != first["CONFIG.generation"]
    assert svc.applied_snapshot["CONFIG.operation_id"] != first["CONFIG.operation_id"]


def test_exited_managed_stimulation_with_failed_cleanup_retains_recovery_and_snapshot(
    stimulation_service, stimulation_snapshot, engine_service,
):
    svc = stimulation_service
    selected = {**stimulation_snapshot, "STIMULATION.auto_spawn": True}
    start(svc, selected)
    first = svc.applied_snapshot
    svc._process_manager.is_alive = False
    svc._process_manager.stop.side_effect = RuntimeError("Stimulation cleanup was not confirmed (exitcode=1)")
    assert svc.owner_identity() == ("stopped", None, None)
    assert engine_service.admission.requires_recovery("stimulation")
    with pytest.raises(RuntimeError, match="exitcode=1"):
        svc.stop()
    assert engine_service.admission.requires_recovery("stimulation")
    assert svc.runtime_package_snapshots == (first,)


def test_failed_managed_launch_preserves_original_error_when_rollback_also_fails(
    stimulation_service, stimulation_snapshot,
):
    svc = stimulation_service
    svc._process_manager.start.side_effect = RuntimeError("renderer init failed")
    svc._process_manager.stop.side_effect = OSError("cleanup failed")
    with pytest.raises(RuntimeError, match="renderer init failed") as exc_info:
        start(svc, {**stimulation_snapshot, "STIMULATION.auto_spawn": True})
    assert any("cleanup failed" in note for note in getattr(exc_info.value, "__notes__", ()))
