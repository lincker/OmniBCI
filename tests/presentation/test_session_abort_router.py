"""Explicit recovery endpoint keeps failed session termination reviewable."""

import pytest


def test_abort_session_and_repeat(api_client):
    sid = api_client.post("/api/v1/sessions", json={"subject_id": "subject"}).json()["data"]["session_id"]
    response = api_client.post(f"/api/v1/sessions/{sid}/abort", json={"reason": "Operator stopped an incomplete run"})
    assert response.status_code == 200
    session = api_client.get(f"/api/v1/sessions/{sid}").json()["data"]
    assert session["state"] == "ended"
    assert session["interruption_reason"] == "Operator stopped an incomplete run"
    assert session["recording"]["status"] == "interrupted"
    assert api_client.post(f"/api/v1/sessions/{sid}/abort", json={"reason": "Retry"}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "  "}, {"reason": None}])
def test_abort_requires_explicit_reason(api_client, body):
    assert api_client.post("/api/v1/sessions/unknown/abort", json=body).status_code == 422


def test_abort_unknown_session(api_client):
    assert api_client.post("/api/v1/sessions/unknown/abort", json={"reason": "Stop"}).status_code == 404
