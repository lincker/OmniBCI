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
        assert "stimulation_connected" in data
        assert "actuation_connected" in data
        assert "actuation_alive" not in data

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


@pytest.mark.parametrize("module", ["processing", "stimulation", "actuation"])
def test_top_level_module_stop_targets_only_requested_module(api_client, engine_service, module):
    with patch.object(engine_service, "stop_module", new_callable=AsyncMock) as operation:
        response = api_client.post(f"/api/v1/{module}/stop")
    assert response.status_code == 200
    operation.assert_awaited_once_with(module)


@pytest.mark.parametrize("module", ["stimulation", "actuation"])
def test_top_level_output_prepare_targets_only_requested_module(api_client, engine_service, module):
    with patch.object(engine_service, "prepare_output", new_callable=AsyncMock) as operation:
        response = api_client.post(f"/api/v1/{module}/prepare")
    assert response.status_code == 200
    operation.assert_awaited_once_with(module)


@pytest.mark.parametrize("path", [
    "/api/v1/engine/modules/processing/start",
    "/api/v1/engine/modules/actuation/stop",
    "/api/v1/engine/modules/stimulation/prepare",
])
def test_legacy_engine_module_routes_are_removed(api_client, path):
    assert api_client.post(path).status_code == 404


def test_actuation_status_mirrors_stimulation_status_contract(
    api_client, engine_service, actuation_service, mock_engine
):
    engine_service._running = True
    engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock(is_alive=lambda: True)
    actuation_service.active = True
    actuation_service.process_manager = MagicMock(is_alive=True, current_effector="omnibci.logger/default@0.1.0")
    mock_engine.is_peer_connected.return_value = True
    response = api_client.get("/api/v1/actuation/status")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "active": True,
        "process_alive": True,
        "engine_connected": True,
        "effector": "omnibci.logger/default@0.1.0",
    }


@pytest.mark.parametrize("execution_unknown", [False, True])
def test_output_delivery_failure_reports_uncertainty(api_client, engine_service, execution_unknown):
    from omnibci.application.services.command_result_service import CommandDeliveryError

    error = CommandDeliveryError(
        "output-command", "SettleActuation",
        "Peer connection failed", execution_unknown=execution_unknown,
    )
    with patch.object(engine_service, "stop_module", new_callable=AsyncMock, side_effect=error):
        response = api_client.post("/api/v1/actuation/stop")

    assert response.status_code == 503
    assert response.json()["code"] == "PEER_DELIVERY_FAILED"
    assert response.json()["detail"]["execution_unknown"] is execution_unknown
