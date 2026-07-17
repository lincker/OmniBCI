"""
Tests for /api/v1/acquisition endpoints.
"""


class TestAcquisitionConnect:
    def test_connect_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/connect")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_connect_dispatches_event(self, api_client, mock_engine):
        api_client.post("/api/v1/acquisition/connect")
        mock_engine.event_bus.put_event.assert_called()

    def test_disconnect_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/disconnect")
        assert resp.status_code == 200

    def test_connect_without_engine_returns_503(self, api_client, engine_service):
        engine_service._engine = None
        resp = api_client.post("/api/v1/acquisition/connect")
        assert resp.status_code == 503


class TestAcquisitionStream:
    def test_start_stream_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/start")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    def test_stop_stream_returns_200(self, api_client, mock_engine):
        resp = api_client.post("/api/v1/acquisition/stream/stop")
        assert resp.status_code == 200

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
        # Projection semantics (方案 A): the command only dispatches intent;
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
