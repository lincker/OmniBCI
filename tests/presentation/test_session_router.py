"""
Tests for /api/v1/sessions endpoints.
"""
from datetime import datetime, timezone

import pytest

from omnibci.shared.events import Event, EventType
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification


def _outcome(sid, clf):
    """P2-8：TrialCompleted 载荷契约为 TrialOutcome（bare Classification 已拒收）。
    trial_number=0 → 读模型按本地计数补算。"""
    return Event(EventType.TrialCompleted,
                 TrialOutcome(session_id=sid, trial_number=0, classification=clf))


def _clf(target_id=3, event_id=101):
    return Classification(
        target_id=target_id, event_id=event_id, confidence=0.8,
        produced_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


class TestCreateSession:
    def test_returns_201(self, api_client):
        resp = api_client.post("/api/v1/sessions")
        assert resp.status_code == 201

    def test_response_has_session_id(self, api_client):
        resp = api_client.post("/api/v1/sessions")
        data = resp.json()["data"]
        assert "session_id" in data
        assert len(data["session_id"]) > 0

    def test_state_is_running(self, api_client):
        resp = api_client.post("/api/v1/sessions")
        assert resp.json()["data"]["state"] == "running"

    def test_with_paradigm(self, api_client):
        resp = api_client.post("/api/v1/sessions", json={"paradigm": "classic40SSVEP"})
        assert resp.json()["data"]["paradigm"] == "classic40SSVEP"

    def test_trial_count_starts_at_zero(self, api_client):
        resp = api_client.post("/api/v1/sessions")
        assert resp.json()["data"]["trial_count"] == 0


class TestListSessions:
    def test_empty_initially(self, api_client):
        resp = api_client.get("/api/v1/sessions")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "sessions" in data
        assert data["total"] == len(data["sessions"])

    def test_created_session_appears_in_list(self, api_client):
        api_client.post("/api/v1/sessions")
        resp = api_client.get("/api/v1/sessions")
        assert resp.json()["data"]["total"] >= 1


class TestGetActiveSession:
    def test_404_when_no_active_session(self, api_client):
        resp = api_client.get("/api/v1/sessions/active")
        assert resp.status_code == 404

    def test_returns_active_after_create(self, api_client):
        created = api_client.post("/api/v1/sessions").json()["data"]
        resp = api_client.get("/api/v1/sessions/active")
        assert resp.status_code == 200
        assert resp.json()["data"]["session_id"] == created["session_id"]


class TestGetSession:
    def test_get_existing_session(self, api_client):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        resp = api_client.get(f"/api/v1/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["data"]["session_id"] == sid

    def test_404_for_unknown_session(self, api_client):
        resp = api_client.get("/api/v1/sessions/does-not-exist")
        assert resp.status_code == 404


class TestEndSession:
    def test_end_returns_200(self, api_client):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        resp = api_client.delete(f"/api/v1/sessions/{sid}")
        assert resp.status_code == 200

    def test_state_becomes_ended(self, api_client):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        api_client.delete(f"/api/v1/sessions/{sid}")
        state = api_client.get(f"/api/v1/sessions/{sid}").json()["data"]["state"]
        assert state == "ended"

    def test_active_cleared_after_end(self, api_client):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        api_client.delete(f"/api/v1/sessions/{sid}")
        resp = api_client.get("/api/v1/sessions/active")
        assert resp.status_code == 404

    def test_end_unknown_session_returns_404(self, api_client):
        resp = api_client.delete("/api/v1/sessions/ghost")
        assert resp.status_code == 404


class TestGetTrials:
    def test_empty_trials(self, api_client):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        resp = api_client.get(f"/api/v1/sessions/{sid}/trials")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_trials_after_event(self, api_client, session_service):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        session_service.on_trial_completed(_outcome(sid, _clf(target_id=4)))
        resp = api_client.get(f"/api/v1/sessions/{sid}/trials")
        trials = resp.json()["data"]
        assert len(trials) == 1
        assert trials[0]["target_id"] == 4

    def test_trials_schema_fields(self, api_client, session_service):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        session_service.on_trial_completed(_outcome(sid, _clf()))
        trial = api_client.get(f"/api/v1/sessions/{sid}/trials").json()["data"][0]
        for field in ("trial_number", "target_id", "event_id", "forced", "produced_at"):
            assert field in trial

    def test_trials_unknown_session_returns_404(self, api_client):
        resp = api_client.get("/api/v1/sessions/ghost/trials")
        assert resp.status_code == 404


class TestGetLastTrial:
    def test_404_with_no_trials(self, api_client):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        resp = api_client.get(f"/api/v1/sessions/{sid}/trials/last")
        assert resp.status_code == 404

    def test_returns_latest_target(self, api_client, session_service):
        sid = api_client.post("/api/v1/sessions").json()["data"]["session_id"]
        session_service.on_trial_completed(_outcome(sid, _clf(target_id=1)))
        session_service.on_trial_completed(_outcome(sid, _clf(target_id=9)))
        last = api_client.get(f"/api/v1/sessions/{sid}/trials/last").json()["data"]
        assert last["target_id"] == 9

    def test_unknown_session_returns_404(self, api_client):
        resp = api_client.get("/api/v1/sessions/ghost/trials/last")
        assert resp.status_code == 404
