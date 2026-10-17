"""Shared session repository contracts and SQLite persistence guarantees."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from omnibci.domain.repositories import SessionRecord, SessionRecordState, TrialRecord
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.repositories.sqlite_session_repository import SQLiteSessionRepository


def _record(session_id="session-1"):
    return SessionRecord(
        session_id=session_id,
        paradigm="classic40SSVEP",
        state=SessionRecordState.RUNNING,
        started_at=datetime(2026, 10, 17, 12, tzinfo=timezone.utc),
        subject_id="subject-1",
        continuation_of="parent-session",
        config_revision="revision-1",
        signal_plan={"channels": ["C3", "C4"], "rate": 250},
        recording={"format": "omnibci.numpy-chunks", "path": "/recordings/recording-1"},
    )


def _trial(number=1):
    return TrialRecord(
        trial_number=number, target_id=2, event_id=100, confidence=0.875,
        forced=False, produced_at=datetime(2026, 10, 17, 12, 0, number, tzinfo=timezone.utc),
        config_revision="revision-2",
    )


@pytest.fixture(params=["memory", "sqlite"])
def repository(request, tmp_path):
    repo = (InMemorySessionRepository() if request.param == "memory"
            else SQLiteSessionRepository(tmp_path / "sessions.sqlite3"))
    yield repo
    if isinstance(repo, SQLiteSessionRepository):
        repo.close()


@pytest.fixture
def sqlite_repository(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.sqlite3")
    yield repo
    repo.close()


def test_repository_round_trip_and_detached_queries(repository):
    record = _record()
    record.append_trial(_trial())
    repository.save(record)
    assert repository.find_by_id(record.session_id) == record
    for result in (repository.find_by_id(record.session_id),
                   repository.find_all()[0], repository.find_active()):
        result.signal_plan["channels"].clear()
        result.trials[0].target_id = 99
        result.recording["format"] = "changed"
    assert repository.find_by_id(record.session_id) == record


def test_repository_update_and_end(repository):
    repository.save(_record())
    result = repository.update("session-1", lambda record: record.append_trial(_trial()))
    result.trials.clear()
    assert repository.find_by_id("session-1").trial_count == 1
    repository.update("session-1", lambda record: record.mark_ended(interruption_reason="Source lost"))
    assert repository.find_active() is None
    ended = repository.find_by_id("session-1")
    assert ended.state == SessionRecordState.ENDED
    assert ended.interruption_reason == "Source lost"
    assert ended.ended_at is not None


def test_repository_missing_update_and_delete(repository):
    called = []
    assert repository.update("missing", lambda record: called.append(record)) is None
    assert called == []
    assert repository.find_by_id("missing") is None
    repository.delete("missing")
    repository.save(_record())
    repository.delete("session-1")
    assert repository.find_all() == []
    assert repository.find_active() is None


def test_repository_upsert_create_and_existing(repository):
    created = []

    def create():
        created.append(True)
        return _record()

    first = repository.upsert("session-1", create, lambda record: record.append_trial(_trial()))
    assert first.trial_count == 1
    second = repository.upsert("session-1", create, lambda record: record.append_trial(_trial(2)))
    assert second.trial_count == 2
    assert created == [True]
    second.signal_plan["channels"].clear()
    assert repository.find_by_id("session-1").signal_plan["channels"] == ["C3", "C4"]


def test_repository_upsert_rejects_wrong_created_id(repository):
    with pytest.raises(ValueError):
        repository.upsert("other-session", _record, lambda record: None)
    assert repository.find_all() == []


def test_repository_mutator_failure_rolls_back_update_and_upsert(repository):
    original = _record()
    repository.save(original)

    def fail(record):
        record.append_trial(_trial())
        record.signal_plan["channels"].clear()
        record.mark_ended()
        raise RuntimeError("Mutator failed")

    with pytest.raises(RuntimeError, match="Mutator failed"):
        repository.update("session-1", fail)
    assert repository.find_by_id("session-1") == original
    with pytest.raises(RuntimeError, match="Mutator failed"):
        repository.upsert("session-2", lambda: _record("session-2"), fail)
    assert repository.find_by_id("session-2") is None


def test_repository_concurrent_appends_and_end_keep_all_committed_trials(repository):
    repository.save(_record())

    def append(_):
        repository.update("session-1", lambda record: record.append_trial(_trial(record.trial_count + 1)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(40)))
    repository.update("session-1", lambda record: record.mark_ended())
    record = repository.find_by_id("session-1")
    assert record.state == SessionRecordState.ENDED
    assert [trial.trial_number for trial in record.trials] == list(range(1, 41))
    with pytest.raises(RuntimeError, match="ended"):
        repository.update("session-1", lambda record: record.append_trial(_trial(41)))


def test_repository_concurrent_end_does_not_lose_successful_appends(repository):
    repository.save(_record())
    start = threading.Event()

    def append(_):
        start.wait()
        try:
            repository.update("session-1", lambda record: record.append_trial(_trial(record.trial_count + 1)))
            return True
        except RuntimeError:
            return False

    def end():
        start.wait()
        repository.update("session-1", lambda record: record.mark_ended())

    with ThreadPoolExecutor(max_workers=8) as pool:
        appends = [pool.submit(append, index) for index in range(4)]
        ending = pool.submit(end)
        appends += [pool.submit(append, index) for index in range(4)]
        start.set()
        successful = sum(future.result() for future in appends)
        ending.result()
    record = repository.find_by_id("session-1")
    assert record.state == SessionRecordState.ENDED
    assert record.trial_count == successful


def test_sqlite_survives_close_and_reopen(tmp_path):
    path = tmp_path / "nested" / "sessions.sqlite3"
    repository = SQLiteSessionRepository(path)
    record = _record()
    record.append_trial(_trial())
    record.mark_ended(interruption_reason="Stopped")
    repository.save(record)
    repository.close()
    reopened = SQLiteSessionRepository(path)
    try:
        assert reopened.find_by_id("session-1") == record
        assert reopened.find_active() is None
    finally:
        reopened.close()


def test_sqlite_constructor_does_not_end_running_sessions(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    repository = SQLiteSessionRepository(path)
    repository.save(_record())
    repository.close()
    reopened = SQLiteSessionRepository(path)
    try:
        assert reopened.find_active().session_id == "session-1"
    finally:
        reopened.close()


def test_sqlite_saved_input_is_detached(sqlite_repository):
    record = _record()
    sqlite_repository.save(record)
    record.signal_plan["channels"].clear()
    record.append_trial(_trial())
    stored = sqlite_repository.find_by_id("session-1")
    assert stored.trials == []
    assert stored.signal_plan["channels"] == ["C3", "C4"]


def test_sqlite_save_rejects_existing_id(sqlite_repository):
    sqlite_repository.save(_record())
    with pytest.raises(ValueError, match="already exists"):
        sqlite_repository.save(_record())


def test_sqlite_ordering_uses_creation_time_with_offset_and_stable_ties(sqlite_repository):
    middle = _record("middle")
    first = _record("first")
    first.started_at -= timedelta(hours=1)
    last = _record("last")
    last.started_at += timedelta(hours=1)
    last.started_at = last.started_at.astimezone(timezone(timedelta(hours=-5)))
    for record in (middle, last, first):
        sqlite_repository.save(record)
    assert [record.session_id for record in sqlite_repository.find_all()] == ["first", "middle", "last"]
    assert sqlite_repository.find_active().session_id == "last"
    sqlite_repository.update("last", lambda record: record.mark_ended())
    assert sqlite_repository.find_active().session_id == "middle"
    sqlite_repository.save(_record("middle-tie"))
    assert sqlite_repository.find_active().session_id == "middle-tie"


def test_sqlite_ordering_retains_microsecond_precision(sqlite_repository):
    first, second = _record("first"), _record("second")
    second.started_at += timedelta(microseconds=1)
    sqlite_repository.save(second)
    sqlite_repository.save(first)
    assert [record.session_id for record in sqlite_repository.find_all()] == ["first", "second"]
    assert sqlite_repository.find_active().session_id == "second"


@pytest.mark.parametrize("operation", ["update", "upsert"])
def test_sqlite_identity_is_immutable(sqlite_repository, operation):
    sqlite_repository.save(_record())
    mutate = lambda record: setattr(record, "session_id", "other-session")
    with pytest.raises(ValueError, match="Session ID must remain"):
        if operation == "update":
            sqlite_repository.update("session-1", mutate)
        else:
            sqlite_repository.upsert("session-1", _record, mutate)
    assert sqlite_repository.find_by_id("other-session") is None
    assert sqlite_repository.find_by_id("session-1") is not None


def test_sqlite_ended_state_cannot_be_reversed_or_receive_new_trials(sqlite_repository):
    record = _record()
    record.mark_ended()
    sqlite_repository.save(record)
    with pytest.raises(ValueError, match="cannot return to running"):
        sqlite_repository.update("session-1", lambda record: setattr(record, "state", SessionRecordState.RUNNING))
    with pytest.raises(ValueError, match="ended session"):
        sqlite_repository.update("session-1", lambda record: record.trials.append(_trial()))
    assert sqlite_repository.find_by_id("session-1") == record


def test_sqlite_duplicate_identical_trial_is_idempotent(sqlite_repository):
    record = _record()
    record.append_trial(_trial())
    sqlite_repository.save(record)
    result = sqlite_repository.update("session-1", lambda record: record.append_trial(_trial()))
    assert result.trial_count == 1
    assert sqlite_repository.find_by_id("session-1") == record


def test_sqlite_conflicting_duplicate_trial_rolls_back_every_field(sqlite_repository):
    original = _record()
    original.append_trial(_trial())
    sqlite_repository.save(original)

    def mutate(record):
        trial = _trial()
        trial.target_id = 99
        record.append_trial(trial)
        record.paradigm = "changed"

    with pytest.raises(ValueError, match="Conflicting content"):
        sqlite_repository.update("session-1", mutate)
    assert sqlite_repository.find_by_id("session-1") == original


@pytest.mark.parametrize("mutation", [
    lambda record: record.trials.clear(),
    lambda record: setattr(record.trials[0], "target_id", 99),
    lambda record: record.trials.reverse(),
])
def test_sqlite_rejects_changes_to_persisted_trial_history(sqlite_repository, mutation):
    record = _record()
    record.trials = [_trial(), _trial(2)]
    sqlite_repository.save(record)
    with pytest.raises(ValueError, match="append-only|must increase"):
        sqlite_repository.update("session-1", mutation)
    assert sqlite_repository.find_by_id("session-1") == record


def test_sqlite_database_constraint_failure_rolls_back_session_and_trials(sqlite_repository):
    original = _record()
    sqlite_repository.save(original)

    def mutate(record):
        record.paradigm = "changed"
        record.append_trial(_trial(0))

    with pytest.raises(sqlite3.IntegrityError):
        sqlite_repository.update("session-1", mutate)
    assert sqlite_repository.find_by_id("session-1") == original
    sqlite_repository.update("session-1", lambda record: record.append_trial(_trial()))
    assert sqlite_repository.find_by_id("session-1").trial_count == 1


def test_sqlite_new_trials_do_not_rewrite_previous_rows(sqlite_repository):
    record = _record()
    record.append_trial(_trial())
    sqlite_repository.save(record)
    connection = sqlite_repository._connection
    connection.execute("""
        CREATE TRIGGER forbid_trial_update BEFORE UPDATE ON trials
        BEGIN SELECT RAISE(ABORT, 'Trial history was updated'); END
    """)
    connection.execute("""
        CREATE TRIGGER forbid_trial_delete BEFORE DELETE ON trials
        BEGIN SELECT RAISE(ABORT, 'Trial history was deleted'); END
    """)
    sqlite_repository.update("session-1", lambda record: record.append_trial(_trial(2)))
    sqlite_repository.update("session-1", lambda record: record.mark_ended())
    assert sqlite_repository.find_by_id("session-1").trial_count == 2


def test_sqlite_delete_cascades_trials_and_preserves_other_sessions(sqlite_repository):
    record = _record()
    record.append_trial(_trial())
    sqlite_repository.save(record)
    sqlite_repository.save(_record("session-2"))
    sqlite_repository.delete("session-1")
    assert sqlite_repository._connection.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 0
    assert sqlite_repository.find_active().session_id == "session-2"


def test_sqlite_connections_serialize_read_modify_write(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    repositories = [SQLiteSessionRepository(path) for _ in range(4)]
    try:
        repositories[0].save(_record())

        def append(repo):
            for _ in range(10):
                repo.update("session-1", lambda record: record.append_trial(_trial(record.trial_count + 1)))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, repositories))
        assert repositories[0].find_by_id("session-1").trial_count == 40
    finally:
        for repo in repositories:
            repo.close()


def test_sqlite_durability_settings_and_schema_version(sqlite_repository):
    connection = sqlite_repository._connection
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLiteSessionRepository.SCHEMA_VERSION


def test_sqlite_rejects_unknown_schema_without_overwriting_it(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES ('kept')")
    with pytest.raises(ValueError, match="Unsupported session database schema"):
        SQLiteSessionRepository(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "kept"


def test_sqlite_close_is_idempotent_and_operations_fail_clearly(sqlite_repository):
    sqlite_repository.close()
    sqlite_repository.close()
    with pytest.raises(RuntimeError, match="repository is closed"):
        sqlite_repository.find_all()
