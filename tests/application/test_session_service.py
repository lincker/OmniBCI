"""
Unit tests for SessionAppService.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.queries.session_queries import (
    GetLastClassificationQuery,
    GetSessionQuery,
    GetSessionTrialsQuery,
    ListSessionsQuery,
    SessionStateDTO,
)
from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification


def _clf(target_id=3, event_id=101, confidence=0.85):
    return Classification(
        target_id=target_id,
        event_id=event_id,
        confidence=confidence,
        produced_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _trial_event(clf):
    return Event(event_type=EventType.TrialCompleted, data=clf)


class TestCreateSession:
    def test_returns_session_id(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        assert isinstance(sid, str) and len(sid) > 0

    def test_session_state_is_running(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.state == SessionStateDTO.RUNNING

    def test_paradigm_stored(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand(paradigm="classic40SSVEP"))
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.paradigm == "classic40SSVEP"

    def test_active_session_id_updated(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        assert session_service.active_session_id == sid

    def test_multiple_sessions_tracked(self, session_service):
        sid1 = session_service.handle_create_session(CreateSessionCommand())
        sid2 = session_service.handle_create_session(CreateSessionCommand())
        dtos = session_service.handle_list_sessions(ListSessionsQuery())
        ids = [d.session_id for d in dtos]
        assert sid1 in ids and sid2 in ids


class TestEndSession:
    def test_state_becomes_ended(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.handle_end_session(EndSessionCommand(sid))
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.state == SessionStateDTO.ENDED

    def test_ended_at_is_set(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.handle_end_session(EndSessionCommand(sid))
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.ended_at is not None

    def test_active_session_cleared(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.handle_end_session(EndSessionCommand(sid))
        assert session_service.active_session_id is None

    def test_end_is_idempotent(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.handle_end_session(EndSessionCommand(sid))
        session_service.handle_end_session(EndSessionCommand(sid))  # no exception

    def test_end_unknown_session_raises(self, session_service):
        with pytest.raises(KeyError):
            session_service.handle_end_session(EndSessionCommand("nonexistent"))


class TestGetSession:
    def test_unknown_session_raises(self, session_service):
        with pytest.raises(KeyError):
            session_service.handle_get_session(GetSessionQuery("missing"))

    def test_initial_trial_count_zero(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.trial_count == 0


class TestOnTrialCompleted:
    def test_trial_appended_to_active_session(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.on_trial_completed(_trial_event(_clf(target_id=5)))
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.trial_count == 1

    def test_trial_fields_correct(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        clf = _clf(target_id=7, event_id=202, confidence=0.9)
        session_service.on_trial_completed(_trial_event(clf))
        trials = session_service.handle_get_trials(GetSessionTrialsQuery(sid))
        assert trials[0].target_id == 7
        assert trials[0].event_id == 202
        assert abs(trials[0].confidence - 0.9) < 1e-6

    def test_trial_numbers_increment(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        for i in range(3):
            session_service.on_trial_completed(_trial_event(_clf(target_id=i)))
        trials = session_service.handle_get_trials(GetSessionTrialsQuery(sid))
        assert [t.trial_number for t in trials] == [1, 2, 3]

    def test_ignores_event_without_active_session(self, session_service):
        # No session created — should not raise
        session_service.on_trial_completed(_trial_event(_clf()))

    def test_ignores_wrong_payload_type(self, session_service, caplog):
        import logging
        session_service.handle_create_session(CreateSessionCommand())
        bad = Event(event_type=EventType.TrialCompleted, data=42)
        with caplog.at_level(logging.ERROR):
            session_service.on_trial_completed(bad)
        assert "Classification" in caplog.text


class TestGetLastClassification:
    def test_returns_none_for_empty_session(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        result = session_service.handle_get_last_classification(GetLastClassificationQuery(sid))
        assert result is None

    def test_returns_latest_trial(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.on_trial_completed(_trial_event(_clf(target_id=1)))
        session_service.on_trial_completed(_trial_event(_clf(target_id=9)))
        last = session_service.handle_get_last_classification(GetLastClassificationQuery(sid))
        assert last.target_id == 9


class TestOnSessionEnded:
    def test_active_session_marked_ended_via_event(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.on_session_ended(
            Event(event_type=EventType.SessionEnded, data=0)
        )
        dto = session_service.handle_get_session(GetSessionQuery(sid))
        assert dto.state == SessionStateDTO.ENDED


# ---------------------------------------------------------------------------
# Session-id unification (docs/session-id-unification.md)
# ---------------------------------------------------------------------------

from omnibci.infrastructure.outbound import TrialOutcome, SessionSummary


def _outcome_event(session_id, trial_number=1, clf=None):
    return Event(
        event_type=EventType.TrialCompleted,
        data=TrialOutcome(session_id=session_id, trial_number=trial_number,
                          classification=clf or _clf()),
    )


class TestProjectionBySessionId:
    def test_trial_routed_by_id_not_by_active(self, session_service):
        """Two RUNNING records: the trial must land on the payload's id,
        not on the most-recently-created (active) one — the old mis-filing bug."""
        sid1 = session_service.handle_create_session(CreateSessionCommand())
        sid2 = session_service.handle_create_session(CreateSessionCommand())
        session_service.on_trial_completed(_outcome_event(sid1))
        assert session_service.handle_get_session(GetSessionQuery(sid1)).trial_count == 1
        assert session_service.handle_get_session(GetSessionQuery(sid2)).trial_count == 0

    def test_unknown_session_id_creates_record(self, session_service):
        session_service.on_trial_completed(_outcome_event("engine-only-id"))
        dto = session_service.handle_get_session(GetSessionQuery("engine-only-id"))
        assert dto.trial_count == 1

    def test_trial_number_taken_from_payload(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand())
        session_service.on_trial_completed(_outcome_event(sid, trial_number=7))
        trials = session_service.handle_get_trials(GetSessionTrialsQuery(sid))
        assert trials[0].trial_number == 7

    def test_session_ended_routed_by_id(self, session_service):
        sid1 = session_service.handle_create_session(CreateSessionCommand())
        sid2 = session_service.handle_create_session(CreateSessionCommand())
        session_service.on_session_ended(Event(
            event_type=EventType.SessionEnded,
            data=SessionSummary(session_id=sid1, total_trials=0),
        ))
        assert session_service.handle_get_session(GetSessionQuery(sid1)).state == SessionStateDTO.ENDED
        assert session_service.handle_get_session(GetSessionQuery(sid2)).state == SessionStateDTO.RUNNING

    def test_on_session_started_creates_missing_record(self, session_service):
        session_service.on_session_started(Event(
            event_type=EventType.SessionStarted, data="engine-started-id"))
        dto = session_service.handle_get_session(GetSessionQuery("engine-started-id"))
        assert dto.state == SessionStateDTO.RUNNING

    def test_on_session_started_is_noop_for_existing(self, session_service):
        sid = session_service.handle_create_session(CreateSessionCommand(paradigm="p"))
        session_service.on_session_started(Event(
            event_type=EventType.SessionStarted, data=sid))
        # paradigm untouched — not overwritten by the projection
        assert session_service.handle_get_session(GetSessionQuery(sid)).paradigm == "p"


class _FakeEnginePort:
    def __init__(self, ready: bool = True):
        self.dispatched: list = []
        self._ready = ready

    def dispatch(self, event):
        if not self._ready:
            raise RuntimeError("engine not initialised")
        self.dispatched.append(event)


class TestEngineDispatchOnLifecycle:
    def _service(self, ready=True):
        from omnibci.application.services.session_service import SessionAppService
        from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
        engine = _FakeEnginePort(ready=ready)
        return SessionAppService(repository=InMemorySessionRepository(),
                                 engine_port=engine), engine

    def test_create_dispatches_start_session_with_id(self):
        svc, engine = self._service()
        sid = svc.handle_create_session(CreateSessionCommand(paradigm="classic40SSVEP"))
        assert len(engine.dispatched) == 1
        ev = engine.dispatched[0]
        assert ev.type == EventType.StartSession
        assert ev.data == {"session_id": sid, "paradigm": "classic40SSVEP"}

    def test_end_dispatches_end_session(self):
        svc, engine = self._service()
        sid = svc.handle_create_session(CreateSessionCommand())
        svc.handle_end_session(EndSessionCommand(sid))
        assert engine.dispatched[-1].type == EventType.EndSession
        assert engine.dispatched[-1].data == sid

    def test_create_rolls_back_on_dispatch_failure(self):
        svc, engine = self._service(ready=False)
        with pytest.raises(RuntimeError):
            svc.handle_create_session(CreateSessionCommand())
        from omnibci.application.queries.session_queries import ListSessionsQuery
        assert svc.handle_list_sessions(ListSessionsQuery()) == []
