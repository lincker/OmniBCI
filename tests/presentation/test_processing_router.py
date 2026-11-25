"""Tests for /api/v1/processing endpoints."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from omnibci.presentation.dependencies import get_runtime_configuration


def test_processing_start_uses_single_server_operation(api_client):
    operation_id = "00000000-0000-0000-0000-000000000001"
    configuration = {"expected_revision": "previous", "settings": {}, "bindings": []}
    result = {
        "module": "processing", "affected_modules": ["processing"], "operation_id": operation_id,
        "plan_id": "internal", "target_revisions": {"processing": "current"},
        "status": "succeeded", "stage": "complete", "error": None, "rolled_back": False,
        "result": {"configuration_applied": True, "action": "started"},
    }
    runtime = SimpleNamespace(execute_current=AsyncMock(return_value=result))
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: runtime
    try:
        response = api_client.post("/api/v1/processing/start", json={
            "operation_id": operation_id, "configuration": configuration,
        })
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration, None)
    assert response.status_code == 200
    assert response.json()["data"] == result
    runtime.execute_current.assert_awaited_once_with(
        "processing", "start", operation_id=operation_id, configuration=configuration,
    )


def test_processing_start_requires_configuration(api_client):
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: SimpleNamespace()
    try:
        assert api_client.post("/api/v1/processing/start").status_code == 422
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration, None)


def test_processing_stop_targets_only_processing_module(api_client, engine_service):
    with patch.object(engine_service, "stop_module", new_callable=AsyncMock) as operation:
        response = api_client.post("/api/v1/processing/stop")
    assert response.status_code == 200
    operation.assert_awaited_once_with("processing")
