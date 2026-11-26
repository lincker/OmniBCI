"""Actuation owns enablement, confirmed settlement and the retained external session."""
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from omnibci.application.configuration.errors import ConfigProfileConflictError, ProfileRevisionConflict
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.shared.events import Event, EventType


def selection(managed=False, revision="a"):
    return {"ACTUATION.auto_spawn": managed, "CONFIG.module_revisions": {"actuation": revision * 64}}


@pytest.mark.asyncio
async def test_managed_process_and_listener_own_one_identity_until_stop(actuation_service, actuation_host):
    owner = actuation_service
    actuation_host.is_peer_connected.return_value = True
    assert not owner.is_active and not owner.process_alive
    assert await owner.start(selection(True)) == "started"
    first = owner.applied_snapshot
    assert first["CONFIG.engine_revision"] is None
    actuation_host.ensure_peer_ready_sync.assert_called_with("actuation", first)
    owner._process_manager.start.assert_called_once_with(settings_snapshot=first)
    assert await owner.start(selection(True)) == "started" and owner.is_active
    await owner.stop()
    owner._command_results.request.assert_called_once_with(EventType.SettleActuation)
    owner._process_manager.stop.assert_called_once()
    assert not owner.is_active and not owner.process_alive and owner.applied_snapshot is None
    await owner.start(selection(True))
    assert owner.applied_snapshot["CONFIG.generation"] != first["CONFIG.generation"]


@pytest.mark.asyncio
async def test_external_settle_and_resume_keep_identity_and_invalidate_queued_trials(actuation_service, actuation_host):
    owner = actuation_service
    assert await owner.start(selection()) == "prepared"
    first = owner.applied_snapshot
    actuation_host.cancel_unaccepted_peer.return_value = False
    actuation_host.is_peer_connected.return_value = True
    assert await owner.start(selection()) == "started"
    event = Event(EventType.TrialCompleted, TrialOutcome("graph", 1, Classification(1, 1)))
    owner.on_trial_completed(event, lambda: True)
    guard = actuation_host.forward_to_peer.call_args.args[-1]
    assert guard()
    await owner.stop()
    assert owner.owner_identity() == ("prepared", first["CONFIG.generation"], "a" * 64)
    assert owner.is_connected and not guard()
    await owner.stop()
    assert await owner.start(selection()) == "started" and not guard()
    assert owner._command_results.request.call_args_list == [
        call(EventType.ResumeActuation), call(EventType.SettleActuation), call(EventType.ResumeActuation)]
    assert owner.applied_snapshot == first
    actuation_host.close_peer_sync.assert_not_called()
    owner._process_manager.start.assert_not_called()
    actuation_host.peer_connection_token.side_effect = lambda _: "new-socket"
    assert not owner.is_active
    assert await owner.start(selection()) == "started"


@pytest.mark.asyncio
async def test_prepared_listener_can_be_cancelled_without_command(actuation_service, actuation_host):
    owner = actuation_service
    owner.prepare_external(selection())
    await owner.stop()
    actuation_host.cancel_unaccepted_peer.assert_called_once_with("actuation")
    owner._command_results.request.assert_not_called()
    assert owner.owner_identity() == ("stopped", None, None)


@pytest.mark.asyncio
async def test_disconnected_unsettled_session_pins_configuration_through_recovery(actuation_service, actuation_host):
    owner = actuation_service
    actuation_host.cancel_unaccepted_peer.return_value = False
    await owner.start(selection())
    original = owner.applied_snapshot
    with pytest.raises(RuntimeError, match="Reconnect"):
        await owner.stop()
    assert owner._admission.recovery_status.keys() == {"actuation"}
    assert owner.runtime_package_snapshots == (original,)
    owner._snapshot_provider = Mock(side_effect=AssertionError("Recovery must use retained settings"))
    owner.prepare_external()
    assert owner.node_configuration_snapshot(Mock()) == ("running", original)
    with pytest.raises(ProfileRevisionConflict):
        await owner.start(selection())
    actuation_host.is_peer_connected.return_value = True
    await owner.stop()
    assert not owner._admission.recovery_required
    with pytest.raises(ConfigProfileConflictError, match="Disconnect"):
        await owner.release()
    actuation_host.is_peer_connected.return_value = False
    await owner.release()
    assert not owner.runtime_package_snapshots


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [EventType.ResumeActuation, EventType.SettleActuation])
async def test_wrong_receipt_retains_external_owner_for_settlement_retry(actuation_service, actuation_host, command):
    owner = actuation_service
    await owner.start(selection())
    actuation_host.is_peer_connected.return_value = True
    actuation_host.cancel_unaccepted_peer.return_value = False
    if command == EventType.SettleActuation:
        await owner.start(selection())
    request = owner._command_results.request
    valid = request.side_effect
    request.side_effect = lambda _: SimpleNamespace(data={"generation": "wrong", "resumed": True})
    with pytest.raises(RuntimeError, match="did not confirm"):
        if command == EventType.ResumeActuation:
            await owner.start(selection())
        else:
            await owner.stop()
    assert not owner.is_active and owner._admission.requires_recovery("actuation")
    request.side_effect = valid
    await owner.stop()
    assert not owner._admission.requires_recovery("actuation") and owner.is_connected


@pytest.mark.asyncio
async def test_managed_settlement_failure_still_reaps_child_and_keeps_pins(actuation_service, actuation_host):
    owner = actuation_service
    actuation_host.is_peer_connected.return_value = True
    await owner.start(selection(True))
    owner._command_results.request.side_effect = RuntimeError("settlement timed out")
    with pytest.raises(RuntimeError, match="timed out"):
        await owner.stop()
    assert not owner.process_alive and owner.runtime_package_snapshots
    assert owner._admission.recovery_status.keys() == {"actuation"}
    assert owner.owner_identity() == ("stopped", None, None)
    await owner.stop()
    assert not owner.runtime_package_snapshots


@pytest.mark.asyncio
async def test_managed_settlement_error_is_preserved_when_process_stop_also_fails(actuation_service, actuation_host):
    owner = actuation_service
    actuation_host.is_peer_connected.return_value = True
    await owner.start(selection(True))
    owner._command_results.request.side_effect = RuntimeError("quiesce failed")
    owner._process_manager.stop.side_effect = OSError("child stop failed")
    with pytest.raises(RuntimeError, match="quiesce failed") as exc_info:
        await owner.stop()
    assert any("child stop failed" in note for note in getattr(exc_info.value, "__notes__", ()))


@pytest.mark.asyncio
async def test_exited_managed_actuation_reports_stopped_with_recovery_and_mints_fresh_identity(
    actuation_service, actuation_host,
):
    owner = actuation_service
    actuation_host.is_peer_connected.return_value = True
    await owner.start(selection(True))
    first = owner.applied_snapshot
    owner._process_manager.is_alive = False
    assert owner.owner_identity() == ("stopped", None, None)
    assert owner._admission.requires_recovery("actuation")
    assert owner.runtime_package_snapshots == (first,)
    with pytest.raises(ProfileRevisionConflict):
        await owner.start(selection(True))
    await owner.stop()
    assert not owner._admission.requires_recovery("actuation")
    await owner.start(selection(True))
    assert owner.applied_snapshot["CONFIG.generation"] != first["CONFIG.generation"]
    assert owner.applied_snapshot["CONFIG.operation_id"] != first["CONFIG.operation_id"]


@pytest.mark.asyncio
async def test_status_blocked_on_process_manager_lock_does_not_stall_ipc_eligibility(
    actuation_service, actuation_host,
):
    import threading
    import time

    owner = actuation_service
    actuation_host.is_peer_connected.return_value = True
    await owner.start(selection(True))
    lifecycle_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    class BlockingManager:
        @property
        def is_alive(self):
            entered.set()
            with lifecycle_lock:
                return True

        @property
        def current_effector(self):
            with lifecycle_lock:
                return "effector"

    owner._process_manager = BlockingManager()
    event = Event(EventType.TrialCompleted, TrialOutcome("graph", 1, Classification(1, 1)))
    owner.on_trial_completed(event, lambda: True)
    eligible = actuation_host.forward_to_peer.call_args.args[-1]

    lifecycle_lock.acquire()
    status_done = threading.Event()

    def poll():
        owner.status(True)
        status_done.set()

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        assert entered.wait(2.0)
        start_time = time.monotonic()
        assert eligible() is True
        elapsed = time.monotonic() - start_time
        assert elapsed < 0.15
    finally:
        lifecycle_lock.release()
        release.set()
        thread.join(2.0)
    assert status_done.is_set()
