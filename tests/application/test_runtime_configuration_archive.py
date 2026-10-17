"""Configuration application archives candidates before workers can use them."""
import pytest

from omnibci.application.services.configuration_archive_service import ConfigurationArchiveService
from omnibci.infrastructure.repositories.sqlite_configuration_archive import SQLiteConfigurationArchive
from tests.application.test_runtime_apply_failures import apply, harness as rebuild_harness
from tests.application.test_runtime_narrow_apply import narrow


@pytest.fixture
def archived(tmp_path):
    repositories = []

    def attach(harness):
        repository = SQLiteConfigurationArchive(tmp_path / f"archive-{len(repositories)}.sqlite3")
        repositories.append(repository)
        service = ConfigurationArchiveService(repository)
        harness.runtime.executor.archive = service
        harness.archive = service
        return harness

    yield attach
    for repository in repositories:
        repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["trial_boundary", "module"])
async def test_candidate_is_archived_before_commands_and_confirmed_after_receipts(narrow, archived, monkeypatch, strategy):
    h = archived(narrow(strategy))
    request = h.commands.request

    def observe(event_type, payload=None, **kwargs):
        archived_candidate = h.archive.get(h.plan.target_revision)
        assert archived_candidate is not None
        assert archived_candidate["applications"][0]["status"] == "pending" or event_type.name == "RestoreSourceCheckpoint"
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", observe)
    operation = await apply(h)
    assert operation["status"] == "succeeded", operation
    stored = h.archive.get_for_session(h.original_session)[0]
    assert stored["config_revision"] == h.plan.target_revision
    application = stored["applications"][0]
    assert application["status"] == "applied"
    assert application["generation"] == "original-generation"
    assert application["receipt"]["processing"]["applied_revision"] == h.plan.target_revision
    assert application["receipt"]["acquisition"]["config_revision"] == h.plan.target_revision


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,expected_status", [
    ("timeout_unknown", "pending"), ("timeout_cancelled", "failed"), ("timeout_applied", "applied"),
])
async def test_timeout_keeps_pending_until_reconciled(narrow, archived, outcome, expected_status):
    h = archived(narrow("trial_boundary"))
    h.commands.stage_outcome = "timeout_cancelled" if outcome == "timeout_cancelled" else "timeout_applied"
    h.commands.cancel_outcome = {"timeout_unknown": "unknown", "timeout_cancelled": "cancelled",
                                 "timeout_applied": "applied"}[outcome]
    await apply(h)
    assert h.archive.get(h.plan.target_revision)["applications"][0]["status"] == expected_status


@pytest.mark.asyncio
async def test_partial_application_never_becomes_confirmed(narrow, archived):
    h = archived(narrow("trial_boundary"))
    h.commands.bad_source_receipt = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.archive.get(h.plan.target_revision)["applications"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_archive_failure_before_apply_does_not_touch_workers(narrow, archived, monkeypatch):
    h = archived(narrow("trial_boundary"))

    def fail(*args, **kwargs):
        raise OSError("Disk full")

    monkeypatch.setattr(h.archive.repository, "stage", fail)
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert h.commands.stage_targets == []
    assert h.engine.applied_revision == h.previous.revision


@pytest.mark.asyncio
async def test_archive_failure_after_apply_requires_reconciliation(narrow, archived, monkeypatch):
    h = archived(narrow("trial_boundary"))

    def fail(*args, **kwargs):
        raise OSError("Disk full")

    monkeypatch.setattr(h.archive.repository, "confirm", fail)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.commands.processing_revision == h.plan.target_revision
    assert h.archive.get(h.plan.target_revision)["applications"][0]["status"] == "pending"


@pytest.mark.asyncio
async def test_rollback_retains_both_acknowledged_pipeline_configurations(narrow, archived, monkeypatch):
    h = archived(narrow("trial_boundary"))

    def fail(*args, **kwargs):
        raise OSError("Activation save failed")

    monkeypatch.setattr(h.profiles.repository, "save_activation", fail)
    operation = await apply(h)
    assert operation["status"] == "failed" and operation["rolled_back"]
    history = h.archive.get_for_session(h.original_session)
    assert [item["config_revision"] for item in history] == [h.plan.target_revision, h.previous.revision]
    assert all(item["applications"][0]["status"] == "applied" for item in history)


@pytest.mark.asyncio
async def test_rebuild_archives_confirmed_new_generation(rebuild_harness, archived):
    h = archived(rebuild_harness())
    operation = await apply(h)
    assert operation["status"] == "succeeded", operation
    application = h.archive.get(h.plan.target_revision)["applications"][0]
    assert application["status"] == "applied"
    assert application["generation"] == h.engine.generation != "original-generation"
    assert application["session_id"] is None
    assert h.archive.get_for_session(h.original_session) == []


@pytest.mark.asyncio
async def test_failed_rebuild_and_successful_rollback_are_recorded_separately(rebuild_harness, archived):
    h = archived(rebuild_harness())
    h.engine.start_failure = h.plan.target_revision
    operation = await apply(h)
    assert operation["status"] == "failed" and operation["rolled_back"], operation
    candidate = h.archive.get(h.plan.target_revision)["applications"][0]
    rollback = h.archive.get(h.previous.revision)["applications"][0]
    assert candidate["status"] == "failed"
    assert rollback["status"] == "applied"
    assert rollback["generation"] == h.engine.generation


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_equivalent_profile_selection_preserves_snapshot_identity(rebuild_harness, archived, running):
    h = archived(rebuild_harness())
    first = h.archive.stage(h.engine.applied_snapshot, context="startup")
    h.archive.confirm(first, h.engine.applied_snapshot, receipt={"ready": True})
    h.engine.is_engine_ready = running
    previous = next(binding for binding in h.previous.bindings if binding.slot == "processing.decoder")
    clone = h.profiles.create("decoder", previous.selection, "Equivalent profile", previous.parameters)
    bindings = [{"slot": binding.slot,
                 "profile_id": clone.profile_id if binding.slot == previous.slot else binding.profile_id}
                for binding in h.previous.bindings]
    h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision,
                            refresh_slots=[previous.slot])
    operation = await apply(h)
    assert operation["status"] == "succeeded", operation
    applications = h.archive.get(h.previous.revision)["applications"]
    assert applications[-1]["status"] == ("applied" if running else "pending")
