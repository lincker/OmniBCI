"""
Tests for /api/v1/engine endpoints.

NeuroEngine is mocked via the engine_service fixture in conftest.py.
"""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


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
        assert data["actuation_alive"] is False
        assert "actuation_connected" in data

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


@pytest.mark.parametrize("module", ["acquisition", "processing", "actuation"])
@pytest.mark.parametrize("action", ["start", "stop"])
def test_module_action_targets_only_requested_module(api_client, engine_service, module, action):
    with patch.object(engine_service, f"{action}_module", new_callable=AsyncMock) as operation:
        response = api_client.post(f"/api/v1/engine/modules/{module}/{action}")
    assert response.status_code == 200
    operation.assert_awaited_once_with(module)


@pytest.mark.parametrize("module", ["unknown", "stimulation", "system"])
def test_module_route_rejects_unsupported_modules(api_client, module):
    assert api_client.post(f"/api/v1/engine/modules/{module}/start").status_code == 422


def test_module_start_reports_foundation_not_ready(api_client, engine_service):
    with patch.object(engine_service, "start_module", new_callable=AsyncMock,
                      side_effect=RuntimeError("Start the engine before starting a module")):
        response = api_client.post("/api/v1/engine/modules/processing/start")
    assert response.status_code == 409
    assert response.json()["code"] == "ENGINE_NOT_READY"
