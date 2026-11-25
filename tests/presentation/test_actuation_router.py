"""Tests for /api/v1/actuation endpoints."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnibci.application.services.command_result_service import CommandDeliveryError
from omnibci.presentation.dependencies import get_runtime_configuration


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


def test_actuation_start_uses_single_server_operation(api_client):
    operation_id = "00000000-0000-0000-0000-000000000001"
    configuration = {"expected_revision": "previous", "settings": {}, "bindings": []}
    result = {
        "module": "actuation", "affected_modules": ["actuation"], "operation_id": operation_id,
        "plan_id": "internal", "target_revisions": {"actuation": "current"},
        "status": "succeeded", "stage": "complete", "error": None, "rolled_back": False,
        "result": {"configuration_applied": True, "action": "started"},
    }
    runtime = SimpleNamespace(execute_current=AsyncMock(return_value=result))
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: runtime
    try:
        response = api_client.post("/api/v1/actuation/start", json={
            "operation_id": operation_id, "configuration": configuration,
        })
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration, None)
    assert response.status_code == 200
    assert response.json()["data"] == result
    runtime.execute_current.assert_awaited_once_with(
        "actuation", "start", operation_id=operation_id, configuration=configuration,
    )


def test_actuation_start_requires_configuration(api_client):
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: SimpleNamespace()
    try:
        assert api_client.post("/api/v1/actuation/start").status_code == 422
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration, None)


def test_actuation_prepare_targets_only_actuation_module(api_client, engine_service):
    with patch.object(engine_service, "prepare_output", new_callable=AsyncMock) as operation:
        response = api_client.post("/api/v1/actuation/prepare")
    assert response.status_code == 200
    operation.assert_awaited_once_with("actuation")


def test_actuation_stop_targets_only_actuation_module(api_client, engine_service):
    with patch.object(engine_service, "stop_module", new_callable=AsyncMock) as operation:
        response = api_client.post("/api/v1/actuation/stop")
    assert response.status_code == 200
    operation.assert_awaited_once_with("actuation")


@pytest.mark.parametrize("execution_unknown", [False, True])
def test_output_delivery_failure_reports_uncertainty(api_client, engine_service, execution_unknown):
    error = CommandDeliveryError(
        "output-command", "SettleActuation",
        "Peer connection failed", execution_unknown=execution_unknown,
    )
    with patch.object(engine_service, "stop_module", new_callable=AsyncMock, side_effect=error):
        response = api_client.post("/api/v1/actuation/stop")

    assert response.status_code == 503
    assert response.json()["code"] == "PEER_DELIVERY_FAILED"
    assert response.json()["detail"]["execution_unknown"] is execution_unknown
