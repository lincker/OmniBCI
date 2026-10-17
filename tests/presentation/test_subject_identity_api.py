"""Session and recording APIs accept optional subjects and expose them in exports."""

import csv
import io
import json
from unittest.mock import Mock

import pytest

from omnibci.application.services.recording_service import RecordingService


def test_subject_round_trip_through_session_queries_and_exports(api_client):
    created = api_client.post("/api/v1/sessions", json={"subject_id": "subject-123"})
    assert created.status_code == 201
    session_id = created.json()["data"]["session_id"]
    assert created.json()["data"]["subject_id"] == "subject-123"

    for path in (f"/api/v1/sessions/{session_id}", "/api/v1/sessions/active"):
        assert api_client.get(path).json()["data"]["subject_id"] == "subject-123"
    assert api_client.get("/api/v1/sessions").json()["data"]["sessions"][0]["subject_id"] == "subject-123"
    exported = api_client.get("/api/v1/sessions/export").json()
    assert exported["sessions"][0]["subject_id"] == "subject-123"
    csv_response = api_client.get("/api/v1/sessions/export?format=csv")
    rows = list(csv.DictReader(io.StringIO(csv_response.text)))
    assert rows[0]["subject_id"] == "subject-123"


@pytest.mark.parametrize("payload", [{}, {"subject_id": None}])
def test_existing_session_clients_can_omit_subject(api_client, payload):
    response = api_client.post("/api/v1/sessions", json=payload)

    assert response.status_code == 201
    assert response.json()["data"]["subject_id"] is None
    assert response.json()["data"]["recording"] is None


def test_recording_metadata_round_trip_through_queries_and_exports(api_client, session_repository):
    created = api_client.post("/api/v1/sessions").json()["data"]
    session_id = created["session_id"]
    recording = {"id": "recording-123", "session_id": session_id, "path": "/recordings/session-123",
                 "status": "recording"}
    session_repository.update(session_id, lambda record: setattr(record, "recording", recording))

    for path in (f"/api/v1/sessions/{session_id}", "/api/v1/sessions/active"):
        assert api_client.get(path).json()["data"]["recording"] == recording
    assert api_client.get("/api/v1/sessions").json()["data"]["sessions"][0]["recording"] == recording
    exported = api_client.get("/api/v1/sessions/export").json()
    assert exported["sessions"][0]["recording"] == recording
    csv_response = api_client.get("/api/v1/sessions/export?format=csv")
    row = next(csv.DictReader(io.StringIO(csv_response.text)))
    assert json.loads(row["recording"]) == recording


@pytest.mark.parametrize("path", ["/api/v1/sessions", "/api/v1/recording/start"])
@pytest.mark.parametrize("subject_id", ["", " \t\n", 123, True, [], {}])
def test_api_rejects_invalid_subject_without_creating_session(api_client, session_service, path, subject_id):
    response = api_client.post(path, json={"subject_id": subject_id})

    assert response.status_code == 422
    assert session_service.active_session_id is None


def test_recording_endpoint_binds_subject_to_session(api_client, session_service):
    acquisition = Mock(spec=["handle_start_stream"])
    api_client.app.state.recording_service = RecordingService(session_service, acquisition)

    response = api_client.post("/api/v1/recording/start", json={"subject_id": "subject-123"})

    assert response.status_code == 201
    session_id = response.json()["data"]["session_id"]
    session = api_client.get(f"/api/v1/sessions/{session_id}").json()["data"]
    assert session["subject_id"] == "subject-123"
