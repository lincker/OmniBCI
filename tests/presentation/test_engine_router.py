"""
Tests for /api/v1/engine endpoints.

NeuroEngine is mocked via the engine_service fixture in conftest.py.
"""
from unittest.mock import AsyncMock, MagicMock, patch


class TestEngineStatus:
    def test_status_when_stopped(self, api_client):
        resp = api_client.get("/api/v1/engine/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        data = body["data"]
        assert "running" in data
        assert "acquisition_alive" in data
        assert "processor_alive" in data

    def test_status_running_true_when_service_running(self, api_client, engine_service):
        engine_service._running = True
        engine_service._accepting_commands = True
        engine_service._engine_thread = MagicMock()
        engine_service._engine_thread.is_alive.return_value = True
        resp = api_client.get("/api/v1/engine/status")
        assert resp.json()["data"]["running"] is True

    def test_status_running_false_when_stopped(self, api_client, engine_service):
        engine_service._running = False
        resp = api_client.get("/api/v1/engine/status")
        assert resp.json()["data"]["running"] is False


class TestEngineStart:
    def test_start_returns_200(self, api_client, engine_service):
        with patch.object(
            engine_service, "handle_start_engine", new_callable=AsyncMock
        ) as mock_start:
            resp = api_client.post("/api/v1/engine/start")
            assert resp.status_code == 200
            assert resp.json()["success"] is True
            mock_start.assert_awaited_once()

    def test_start_conflict_when_already_running(self, api_client, engine_service):
        engine_service._running = True
        # handle_start_engine logs and returns without raising when already running
        with patch.object(engine_service, "handle_start_engine", new_callable=AsyncMock):
            resp = api_client.post("/api/v1/engine/start")
            assert resp.status_code == 200

    def test_start_returns_error_on_runtime_error(self, api_client, engine_service):
        with patch.object(
            engine_service,
            "handle_start_engine",
            new_callable=AsyncMock,
            side_effect=RuntimeError("not initialised"),
        ):
            resp = api_client.post("/api/v1/engine/start")
            assert resp.status_code == 409


class TestEngineStop:
    def test_stop_returns_200(self, api_client, engine_service):
        with patch.object(
            engine_service, "handle_stop_engine", new_callable=AsyncMock
        ) as mock_stop:
            resp = api_client.post("/api/v1/engine/stop")
            assert resp.status_code == 200
            mock_stop.assert_awaited_once()


class TestEngineProcessorEndpointsRemoved:
    """The processor start/stop endpoints were removed (dead no-op event /
    irreversible worker kill) — workers auto-start with the engine."""

    def test_processor_start_is_gone(self, api_client):
        assert api_client.post("/api/v1/engine/processor/start").status_code == 404

    def test_processor_stop_is_gone(self, api_client):
        assert api_client.post("/api/v1/engine/processor/stop").status_code == 404
