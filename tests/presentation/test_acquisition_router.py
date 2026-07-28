"""
Tests for /api/v1/acquisition endpoints.
"""
import pytest
from unittest.mock import MagicMock


@pytest.fixture(autouse=True)
def _running_engine(engine_service):
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True


class TestAcquisitionConnect:
    def test_connect_returns_202_accepted(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/connect")
        assert resp.status_code == 202
        assert resp.json()["success"] is True
        assert resp.json()["message"] == "Data provider connection requested"

    def test_connect_dispatches_event(self, api_client, mock_engine):
        api_client.post("/api/v1/acquisition/connect")
        mock_engine.event_bus.put_event.assert_called()

    def test_disconnect_returns_202_accepted(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/disconnect")
        assert resp.status_code == 202
        assert resp.json()["message"] == "Data provider disconnection requested"

    def test_connect_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/acquisition/connect")
        assert resp.status_code == 503


class TestAcquisitionStream:
    def test_start_stream_returns_202_accepted(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/start")
        assert resp.status_code == 202
        assert resp.json()["success"] is True
        assert resp.json()["message"] == "Stream start requested"

    def test_stop_stream_returns_202_accepted(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/stop")
        assert resp.status_code == 202
        assert resp.json()["message"] == "Stream stop requested"

    def test_start_stream_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/acquisition/stream/start")
        assert resp.status_code == 503


class TestAcquisitionStatus:
    def test_initial_status(self, api_client):
        resp = api_client.get("/api/v1/acquisition/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["connected"] is False
        assert data["streaming"] is False

    def test_connect_alone_does_not_flip_status(self, api_client, acquisition_service, mock_engine):
        # Projection semantics (option A): the command only dispatches intent;
        # status flips when the worker reports back via AcquisitionState.
        api_client.post("/api/v1/acquisition/connect")
        resp = api_client.get("/api/v1/acquisition/status")
        assert resp.json()["data"]["connected"] is False

    def test_status_reflects_worker_snapshot(self, api_client, acquisition_service, mock_engine):
        from omnibci.application.events import AcquisitionSnapshot
        from omnibci.shared.events import Event, EventType

        api_client.post("/api/v1/acquisition/connect")
        api_client.post("/api/v1/acquisition/stream/start")
        acquisition_service.on_acquisition_state(
            Event(EventType.AcquisitionState,
                  AcquisitionSnapshot(connected=True, streaming=True)))
        resp = api_client.get("/api/v1/acquisition/status")
        data = resp.json()["data"]
        assert data["connected"] is True
        assert data["streaming"] is True

    def test_status_schema_fields(self, api_client):
        resp = api_client.get("/api/v1/acquisition/status")
        data = resp.json()["data"]
        for field in ("connected", "streaming"):
            assert field in data
