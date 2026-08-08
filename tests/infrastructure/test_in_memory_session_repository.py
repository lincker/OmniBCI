"""
Tests for InMemorySessionRepository.

Covers all SessionRepository contract methods plus thread-safety.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from omnibci.domain.repositories import SessionRecord, SessionRecordState, TrialRecord
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository


def _record(session_id="s1", state=SessionRecordState.RUNNING, paradigm=None) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        paradigm=paradigm,
        state=state,
        started_at=datetime.now(timezone.utc),
    )


def _trial(n=1, target_id=0) -> TrialRecord:
    return TrialRecord(
        trial_number=n,
        target_id=target_id,
        event_id=100,
        confidence=0.9,
        forced=False,
        produced_at=datetime.now(timezone.utc),
    )


class TestSaveAndFindById:
    def test_saved_record_retrievable(self):
        repo = InMemorySessionRepository()
        repo.save(_record("abc"))
        assert repo.find_by_id("abc") is not None

    def test_unknown_id_returns_none(self):
        repo = InMemorySessionRepository()
        assert repo.find_by_id("ghost") is None

    def test_save_replaces_existing(self):
        repo = InMemorySessionRepository()
        repo.save(_record("x", paradigm="old"))
        updated = _record("x", paradigm="new")
        repo.save(updated)
        assert repo.find_by_id("x").paradigm == "new"

    def test_returns_deep_copy(self):
        """Mutating the returned record must not affect the store."""
        repo = InMemorySessionRepository()
        repo.save(_record("z"))
        copy1 = repo.find_by_id("z")
        copy1.paradigm = "mutated"
        assert repo.find_by_id("z").paradigm is None


class TestFindAll:
    def test_empty_initially(self):
        assert InMemorySessionRepository().find_all() == []

    def test_preserves_insertion_order(self):
        repo = InMemorySessionRepository()
        for sid in ["a", "b", "c"]:
            repo.save(_record(sid))
        ids = [r.session_id for r in repo.find_all()]
        assert ids == ["a", "b", "c"]

    def test_count_matches_saves(self):
        repo = InMemorySessionRepository()
        repo.save(_record("1"))
        repo.save(_record("2"))
        assert len(repo.find_all()) == 2

    def test_update_does_not_add_duplicate(self):
        repo = InMemorySessionRepository()
        repo.save(_record("dup"))
        repo.save(_record("dup"))   # update
        assert len(repo.find_all()) == 1


class TestFindActive:
    def test_none_when_empty(self):
        assert InMemorySessionRepository().find_active() is None

    def test_running_session_is_active(self):
        repo = InMemorySessionRepository()
        repo.save(_record("r", state=SessionRecordState.RUNNING))
        assert repo.find_active().session_id == "r"

    def test_ended_session_not_active(self):
        repo = InMemorySessionRepository()
        repo.save(_record("e", state=SessionRecordState.ENDED))
        assert repo.find_active() is None

    def test_latest_running_is_active(self):
        repo = InMemorySessionRepository()
        repo.save(_record("first"))
        repo.save(_record("second"))
        assert repo.find_active().session_id == "second"

    def test_active_updates_when_session_ended(self):
        repo = InMemorySessionRepository()
        rec = _record("only")
        repo.save(rec)
        rec.mark_ended()
        repo.save(rec)
        assert repo.find_active() is None


class TestDelete:
    def test_delete_removes_record(self):
        repo = InMemorySessionRepository()
        repo.save(_record("del"))
        repo.delete("del")
        assert repo.find_by_id("del") is None

    def test_delete_unknown_is_noop(self):
        InMemorySessionRepository().delete("nope")  # no exception

    def test_delete_updates_active(self):
        repo = InMemorySessionRepository()
        repo.save(_record("only"))
        repo.delete("only")
        assert repo.find_active() is None


class TestSessionRecordMutations:
    def test_append_trial(self):
        rec = _record()
        rec.append_trial(_trial(n=1, target_id=3))
        assert rec.trial_count == 1
        assert rec.last_trial.target_id == 3

    def test_append_to_ended_raises(self):
        rec = _record(state=SessionRecordState.ENDED)
        with pytest.raises(RuntimeError, match="ended"):
            rec.append_trial(_trial())

    def test_mark_ended_idempotent(self):
        rec = _record()
        rec.mark_ended()
        first_ended_at = rec.ended_at
        rec.mark_ended()
        assert rec.ended_at == first_ended_at

    def test_mark_ended_sets_timestamp(self):
        rec = _record()
        assert rec.ended_at is None
        rec.mark_ended()
        assert rec.ended_at is not None


class TestThreadSafety:
    def test_concurrent_saves_no_data_loss(self):
        repo = InMemorySessionRepository()
        errors = []

        def writer(i):
            try:
                repo.save(_record(f"s{i}"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors
        assert len(repo.find_all()) == 50

# ---------------------------------------------------------------------------
# P1-4: atomic update / upsert primitives
# ---------------------------------------------------------------------------

class TestAtomicUpdate:
    def test_update_mutates_and_returns_copy(self):
        repo = InMemorySessionRepository()
        repo.save(_record("s1"))
        out = repo.update("s1", lambda r: r.append_trial(_trial(1)))
        assert out.trial_count == 1
        assert repo.find_by_id("s1").trial_count == 1
        out.trials.clear()  # the returned copy cannot mutate stored state
        assert repo.find_by_id("s1").trial_count == 1

    def test_update_unknown_returns_none_without_calling_mutator(self):
        repo = InMemorySessionRepository()
        called = []
        assert repo.update("ghost", lambda r: called.append(1)) is None
        assert called == []

    def test_update_is_all_or_nothing_on_mutator_error(self):
        repo = InMemorySessionRepository()
        rec = _record("s1")
        rec.mark_ended()
        repo.save(rec)
        with pytest.raises(RuntimeError):
            repo.update("s1", lambda r: r.append_trial(_trial(1)))  # ended → raises
        assert repo.find_by_id("s1").trial_count == 0  # no partial mutation leaked

    def test_update_maintains_active_tracking(self):
        repo = InMemorySessionRepository()
        repo.save(_record("s1"))
        assert repo.find_active().session_id == "s1"
        repo.update("s1", lambda r: r.mark_ended())
        assert repo.find_active() is None


class TestUpsert:
    def test_creates_when_missing(self):
        repo = InMemorySessionRepository()
        out = repo.upsert("new", lambda: _record("new"),
                          lambda r: r.append_trial(_trial(1)))
        assert out.trial_count == 1
        assert repo.find_by_id("new") is not None

    def test_mutates_existing_without_calling_create(self):
        repo = InMemorySessionRepository()
        repo.save(_record("s1", paradigm="p"))
        created = []
        out = repo.upsert("s1", lambda: created.append(1) or _record("s1"),
                          lambda r: r.append_trial(_trial(1)))
        assert created == []           # existing records skip the create callback
        assert out.paradigm == "p"     # existing fields survive the mutation
        assert out.trial_count == 1

    def test_create_with_wrong_id_rejected(self):
        repo = InMemorySessionRepository()
        with pytest.raises(ValueError):
            repo.upsert("a", lambda: _record("b"), lambda r: None)


class TestConcurrentAppends:
    """Regression coverage for P1-4 concurrent lost updates.

    The old find-copy-save flow lost trials across threads. ``update()`` runs
    the mutator under the repository lock, so the final count must be exact.
    """

    def test_no_lost_trials_under_concurrent_update(self):
        repo = InMemorySessionRepository()
        repo.save(_record("s1"))
        threads_n, per_thread = 8, 50

        def worker():
            for _ in range(per_thread):
                repo.update("s1", lambda r: r.append_trial(
                    _trial(r.trial_count + 1)))

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert repo.find_by_id("s1").trial_count == threads_n * per_thread

    def test_end_during_concurrent_appends_is_consistent(self):
        """Concurrent end and append operations preserve the ENDED state."""
        repo = InMemorySessionRepository()
        repo.save(_record("s1"))
        stop = threading.Event()

        def appender():
            while not stop.is_set():
                try:
                    repo.update("s1", lambda r: r.append_trial(
                        _trial(r.trial_count + 1)))
                except RuntimeError:
                    return  # appends are correctly rejected after session end

        ts = [threading.Thread(target=appender) for _ in range(4)]
        for t in ts: t.start()
        repo.update("s1", lambda r: r.mark_ended())
        stop.set()
        for t in ts: t.join()
        final = repo.find_by_id("s1")
        assert final.state == SessionRecordState.ENDED
        assert final.ended_at is not None
