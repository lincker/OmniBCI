"""Persistent session lifecycle and ownership across backend instances."""

from datetime import datetime, timezone

import pytest

from omnibci.bootstrap import _build_session_repository, _close_previous_sessions
from omnibci.domain.repositories import SessionRecord, SessionRecordState
from omnibci.infrastructure.storage_lock import StorageLease, StorageInUseError


def test_restart_closes_old_session_and_preserves_subject_and_file_link(tmp_path):
    repository = _build_session_repository("sqlite")
    raw = tmp_path / "recordings" / "run"
    raw.mkdir(parents=True)
    record = SessionRecord("old", "ssvep", SessionRecordState.RUNNING,
                           datetime.now(timezone.utc), subject_id="subject-1",
                           recording={"path": str(raw), "status": "recording"})
    repository.save(record)
    repository.close()
    reopened = _build_session_repository("sqlite")
    try:
        _close_previous_sessions(reopened)
        restored = reopened.find_by_id("old")
        assert restored.state is SessionRecordState.ENDED
        assert restored.subject_id == "subject-1"
        assert restored.recording["path"] == str(raw)
        assert restored.interruption_reason == "Backend restarted before the session ended"
        assert reopened.find_active() is None
    finally:
        reopened.close()


def test_live_writer_prevents_startup_from_ending_any_old_session(tmp_path):
    repository = _build_session_repository("sqlite")
    raw = tmp_path / "recordings" / "live"
    raw.mkdir(parents=True)
    for sid, recording in (("first", None), ("live", {"path": str(raw)})):
        repository.save(SessionRecord(sid, None, SessionRecordState.RUNNING,
                                      datetime.now(timezone.utc), recording=recording))
    try:
        with StorageLease(raw / ".writer.lock"):
            with pytest.raises(StorageInUseError):
                _close_previous_sessions(repository)
        assert all(record.state is SessionRecordState.RUNNING for record in repository.find_all())
    finally:
        repository.close()


def test_storage_lease_prevents_other_owner_and_releases_on_close(tmp_path):
    path = tmp_path / "store.lock"
    with StorageLease(path):
        with pytest.raises(StorageInUseError):
            StorageLease(path)
    with StorageLease(path):
        pass


def test_database_override_requires_absolute_path(monkeypatch):
    monkeypatch.setenv("OMNIBCI_DATABASE_PATH", "relative.sqlite3")
    with pytest.raises(ValueError, match="absolute"):
        _build_session_repository("sqlite")
