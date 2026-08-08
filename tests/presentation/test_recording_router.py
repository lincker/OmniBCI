"""Tests for the recording orchestration API."""

from unittest.mock import MagicMock

import pytest

from omnibci.application.services.recording_service import (
    NoActiveRecordingError,
    RecordingConflictError,
    RecordingService,
)


@pytest.fixture()
def recording_service(api_client):
    """Replace the application recording service with an isolated mock."""
    service = MagicMock(spec=RecordingService)
    api_client.app.state.recording_service = service
    return service


class TestStartRecording:
    def test_start_returns_created_session(self, api_client, recording_service):
        recording_service.start.return_value = "session-123"

        response = api_client.post(
            "/api/v1/recording/start",
            json={"paradigm": "classic40SSVEP"},
        )

        assert response.status_code == 201
        assert response.json() == {
            "success": True,
            "message": "Recording started",
            "data": {"session_id": "session-123"},
        }
        recording_service.start.assert_called_once_with(paradigm="classic40SSVEP")

    def test_start_without_body_uses_default_paradigm(
        self,
        api_client,
        recording_service,
    ):
        recording_service.start.return_value = "session-123"

        response = api_client.post("/api/v1/recording/start")

        assert response.status_code == 201
        recording_service.start.assert_called_once_with(paradigm=None)

    def test_start_reports_active_recording_conflict(
        self,
        api_client,
        recording_service,
    ):
        recording_service.start.side_effect = RecordingConflictError(
            "A recording is already active."
        )

        response = api_client.post("/api/v1/recording/start")

        assert response.status_code == 409
        assert response.json() == {
            "success": False,
            "code": "ALREADY_RECORDING",
            "message": "A recording is already active.",
            "detail": None,
        }

    def test_start_reports_engine_unavailable(self, api_client, recording_service):
        recording_service.start.side_effect = RuntimeError("Engine is not ready.")

        response = api_client.post("/api/v1/recording/start")

        assert response.status_code == 503
        assert response.json() == {
            "success": False,
            "code": "ENGINE_NOT_READY",
            "message": "Engine is not ready.",
            "detail": None,
        }


class TestStopRecording:
    def test_stop_returns_ended_session(self, api_client, recording_service):
        recording_service.stop.return_value = "session-123"

        response = api_client.post("/api/v1/recording/stop")

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "message": "Recording stopped",
            "data": {"session_id": "session-123"},
        }
        recording_service.stop.assert_called_once_with()

    def test_stop_reports_missing_active_recording(
        self,
        api_client,
        recording_service,
    ):
        recording_service.stop.side_effect = NoActiveRecordingError(
            "No active recording to stop."
        )

        response = api_client.post("/api/v1/recording/stop")

        assert response.status_code == 409
        assert response.json() == {
            "success": False,
            "code": "NO_ACTIVE_RECORDING",
            "message": "No active recording to stop.",
            "detail": None,
        }
