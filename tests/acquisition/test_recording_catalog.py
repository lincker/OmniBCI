"""Startup/abort reconciliation preserves records and live writer ownership."""

from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pytest

from omnibci.domain.repositories import SessionRecord, SessionRecordState, TrialRecord
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition import recording_catalog as module
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder
from omnibci.infrastructure.acquisition.recording_catalog import reconcile_sessions, recover_session_recording
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.storage_lock import StorageInUseError, StorageLease


def record(session_id="session-1", **changes):
    return SessionRecord(session_id, "test", SessionRecordState.RUNNING,
                         datetime.now(timezone.utc), subject_id="subject-1", **changes)


def recorded(tmp_path, session_id="session-1", *, status="completed", **start_metadata):
    writer = LocalSignalRecorder(tmp_path)
    info = writer.start({"session_id": session_id, "subject_id": "subject-1", **start_metadata})
    writer.append(SignalChunk(1234, 0, np.array([[1., 2.], [0., 9.]])))
    info = writer.finish(status=status)
    return Path(info["path"]), info


def stale_manifest(path):
    metadata = json.loads((path / "metadata.json").read_text())
    metadata["status"] = "recording"
    (path / "metadata.json").write_text(json.dumps(metadata))


def test_restart_reconciles_raw_recording_and_preserves_trial_identity(tmp_path):
    path, info = recorded(tmp_path)
    stale_manifest(path)
    info["status"] = "recording"
    trial = TrialRecord(1, 3, 9, 0.8, False, datetime.now(timezone.utc), config_revision="revision-1")
    old = record(recording=info, trials=[trial], signal_plan={"revision": "revision-1"})
    repository = InMemorySessionRepository()
    repository.save(old)
    reconcile_sessions(repository)
    restored = repository.find_by_id(old.session_id)
    assert restored.state is SessionRecordState.ENDED
    assert restored.subject_id == old.subject_id
    assert restored.started_at == old.started_at
    assert restored.trials == [trial]
    assert restored.signal_plan == old.signal_plan
    assert restored.recording["id"] == info["id"]
    assert restored.recording["status"] == "interrupted"
    assert restored.recording["chunk_count"] == 1
    assert restored.recording["recovery"]["valid_prefix"]["sample_count"] == 2
    assert restored.interruption_reason == module.RESTART_REASON
    assert repository.find_active() is None
    reconcile_sessions(repository)
    assert repository.find_by_id(old.session_id) == restored


def test_ended_session_with_pending_recording_is_reconciled_without_changing_end_time(tmp_path):
    path, info = recorded(tmp_path)
    stale_manifest(path)
    info["status"] = "pending"
    old = record(recording=info)
    old.mark_ended(interruption_reason="User aborted")
    repository = InMemorySessionRepository()
    repository.save(old)
    reconcile_sessions(repository)
    restored = repository.find_by_id(old.session_id)
    assert restored.ended_at == old.ended_at
    assert restored.interruption_reason == "User aborted"
    assert restored.recording["status"] == "interrupted"


def test_live_writer_preflight_prevents_all_metadata_and_database_changes(tmp_path):
    first_path, first_info = recorded(tmp_path, "first")
    stale_manifest(first_path)
    first_info["status"] = "recording"
    live = LocalSignalRecorder(tmp_path)
    live_info = live.start({"session_id": "live"})
    repository = InMemorySessionRepository()
    repository.save(record("first", recording=first_info))
    repository.save(record("live", recording={**live_info, "status": "recording"}))
    before = repository.find_all()
    manifest = (first_path / "metadata.json").read_bytes()
    try:
        with pytest.raises(StorageInUseError):
            reconcile_sessions(repository)
        assert repository.find_all() == before
        assert (first_path / "metadata.json").read_bytes() == manifest
    finally:
        live.finish()


def test_late_ownership_conflict_prevents_all_database_changes(tmp_path, monkeypatch):
    repository = InMemorySessionRepository()
    for name in ("first", "second"):
        _, info = recorded(tmp_path, name)
        repository.save(record(name, recording=info))
    before = repository.find_all()
    recover = module.recover_session_recording

    def conflict(value):
        if value.session_id == "second":
            raise StorageInUseError("Writer is still alive")
        return recover(value)

    monkeypatch.setattr(module, "recover_session_recording", conflict)
    with pytest.raises(StorageInUseError):
        reconcile_sessions(repository)
    assert repository.find_all() == before


@pytest.mark.parametrize("change,error", [
    ({"format": "future.format"}, "Unsupported recording"),
    ({"format_version": 2}, "Unsupported recording"),
    ({"session_id": "different"}, "different session"),
    ({"id": "different"}, "different recording ID"),
])
def test_unknown_or_mismatched_recording_is_explicit_and_source_is_untouched(tmp_path, change, error):
    path, info = recorded(tmp_path)
    metadata = json.loads((path / "metadata.json").read_text())
    metadata.update(change)
    (path / "metadata.json").write_text(json.dumps(metadata))
    before = {name: (path / name).read_bytes() for name in ("metadata.json", "chunks.npy", "chunks.jsonl")}
    restored = recover_session_recording(record(recording=info))
    assert restored["status"] == "failed"
    assert restored["id"] == info["id"]
    assert error in restored["recovery_error"]
    assert before == {name: (path / name).read_bytes() for name in before}


def test_corrupt_confirmed_prefix_marks_descriptor_failed_without_rewriting_data(tmp_path):
    path, info = recorded(tmp_path)
    (path / "chunks.npy").write_bytes(b"broken array")
    restored = recover_session_recording(record(recording=info))
    assert restored["status"] == "failed"
    assert "Corruption inside" in restored["recovery_error"]
    assert (path / "chunks.npy").read_bytes() == b"broken array"


@pytest.mark.parametrize("info,error", [
    ({"status": "pending"}, "not acknowledged"),
    ({"path": "relative", "status": "recording"}, "absolute directory"),
])
def test_unknown_location_never_fabricates_a_recording(tmp_path, info, error):
    restored = recover_session_recording(record(recording=info))
    assert restored["status"] == "failed"
    assert error in restored["recovery_error"]
    assert "id" not in restored
    assert "chunk_count" not in restored
    assert list(tmp_path.iterdir()) == []


def test_missing_location_and_legacy_no_recording_are_distinct(tmp_path):
    assert recover_session_recording(record()) is None
    path = tmp_path / "missing"
    restored = recover_session_recording(record(recording={"path": str(path), "status": "pending"}))
    assert restored["status"] == "failed"
    assert "unavailable" in restored["recovery_error"]
    assert not path.exists()


def test_original_failed_state_and_error_survive_crash_recovery(tmp_path):
    path, info = recorded(tmp_path)
    stale_manifest(path)
    info.update(status="failed", error="Disk full")
    restored = recover_session_recording(record(recording=info))
    assert restored["status"] == "failed"
    assert restored["error"] == "Disk full"
    assert restored["recovery"]["status"] == "interrupted"


def test_abort_helper_rejects_live_writer_before_even_inspecting_format(tmp_path):
    path, info = recorded(tmp_path)
    (path / "metadata.json").write_text("invalid metadata")
    with StorageLease(path / ".writer.lock"):
        with pytest.raises(StorageInUseError):
            recover_session_recording(record(recording=info))


def test_lost_begin_ack_finds_only_existing_sessions_unique_file(tmp_path):
    path, info = recorded(tmp_path / "recordings")
    stale_manifest(path)
    orphan_path, _ = recorded(tmp_path / "recordings", "unknown-session")
    orphan_before = (orphan_path / "metadata.json").read_bytes()
    repository = InMemorySessionRepository()
    repository.save(record(recording={"session_id": "session-1", "status": "pending"}))
    reconcile_sessions(repository)
    restored = repository.find_by_id("session-1")
    assert restored.recording["path"] == str(path)
    assert restored.recording["id"] == info["id"]
    assert restored.recording["status"] == "interrupted"
    assert len(repository.find_all()) == 1
    assert (orphan_path / "metadata.json").read_bytes() == orphan_before


def test_lost_begin_ack_recovery_preserves_replay_source_identity(tmp_path):
    source = {"recording_id": "original-recording", "session_id": "original-session",
              "format": "omnibci.numpy-chunks", "format_version": 1, "partial": False}
    path, info = recorded(tmp_path / "recordings", source_recording=source)
    stale_manifest(path)
    repository = InMemorySessionRepository()
    repository.save(record(recording={"session_id": "session-1", "status": "pending"}))
    reconcile_sessions(repository)
    restored = repository.find_by_id("session-1")
    assert restored.recording["source_recording"] == source
    assert restored.recording["id"] == info["id"] != source["recording_id"]
    restored.recording["source_recording"]["recording_id"] = "changed"
    assert json.loads((path / "metadata.json").read_text())["source_recording"] == source


def test_lost_begin_ack_live_writer_blocks_all_startup_changes(tmp_path):
    writer = LocalSignalRecorder(tmp_path / "recordings")
    writer.start({"session_id": "session-1"})
    repository = InMemorySessionRepository()
    repository.save(record("first"))
    repository.save(record(recording={"session_id": "session-1", "status": "pending"}))
    before = repository.find_all()
    try:
        with pytest.raises(StorageInUseError):
            reconcile_sessions(repository)
        assert repository.find_all() == before
    finally:
        writer.finish()


@pytest.mark.parametrize("operation", ["startup", "abort"])
def test_pending_without_path_blocks_on_live_writer_before_first_manifest(tmp_path, operation):
    path = tmp_path / "recordings" / "unacknowledged"
    pending = record(recording={"session_id": "session-1", "status": "pending"})
    repository = InMemorySessionRepository()
    repository.save(record("first"))
    repository.save(pending)
    before = repository.find_all()
    with StorageLease(path / ".writer.lock"):
        assert not (path / "metadata.json").exists()
        with pytest.raises(StorageInUseError):
            if operation == "startup":
                reconcile_sessions(repository)
            else:
                recover_session_recording(pending)
    assert repository.find_all() == before


def test_ambiguous_lost_ack_keeps_explicit_error_without_linking_or_mutating_files(tmp_path):
    paths = [recorded(tmp_path / "recordings")[0] for _ in range(2)]
    for path in paths:
        stale_manifest(path)
    before = [(path / "metadata.json").read_bytes() for path in paths]
    restored = recover_session_recording(record(recording={"status": "pending"}))
    assert restored["status"] == "failed"
    assert "ambiguous" in restored["recovery_error"]
    assert "path" not in restored
    assert [(path / "metadata.json").read_bytes() for path in paths] == before


def test_identity_is_rechecked_inside_the_recovery_lease(tmp_path, monkeypatch):
    path, info = recorded(tmp_path)
    stale_manifest(path)
    recover = module.recover_recording

    def swapped(path, **expected):
        metadata = json.loads((path / "metadata.json").read_text())
        metadata["session_id"] = "different-session"
        (path / "metadata.json").write_text(json.dumps(metadata))
        return recover(path, **expected)

    monkeypatch.setattr(module, "recover_recording", swapped)
    restored = recover_session_recording(record(recording=info))
    assert restored["status"] == "failed"
    assert "different session" in restored["recovery_error"]
    assert json.loads((path / "metadata.json").read_text())["status"] == "recording"
