"""User actions submit complete current values to one owned backend use case."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnibci.presentation.routers.runtime_config import get_runtime_configuration


@pytest.fixture(autouse=True)
def available_runtime(api_client):
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: SimpleNamespace()
    yield
    api_client.app.dependency_overrides.pop(get_runtime_configuration, None)


@pytest.mark.parametrize("path,module,action", [
    ("/acquisition/connect", "acquisition", "connect"),
    ("/engine/modules/processing/start", "processing", "start"),
    ("/engine/modules/actuation/start", "actuation", "start"),
    ("/stimulation/start", "stimulation", "start"),
])
def test_current_action_uses_single_backend_operation(api_client, path, module, action):
    operation_id = "00000000-0000-0000-0000-000000000001"
    configuration = {"expected_revision": "previous", "settings": {}, "bindings": []}
    result = {
        "module": module, "affected_modules": [module], "operation_id": operation_id,
        "plan_id": "internal", "target_revisions": {module: "current"},
        "status": "succeeded", "stage": "complete", "error": None, "rolled_back": False,
        "result": {"configuration_applied": True, "action": "connected" if action == "connect" else "started"},
    }
    runtime = SimpleNamespace(execute_current=AsyncMock(return_value=result))
    api_client.app.dependency_overrides[get_runtime_configuration] = lambda: runtime
    try:
        response = api_client.post(f"/api/v1{path}", json={
            "operation_id": operation_id, "configuration": configuration,
        })
    finally:
        api_client.app.dependency_overrides.pop(get_runtime_configuration)
    assert response.status_code == 200
    assert response.json()["data"] == result
    runtime.execute_current.assert_awaited_once_with(
        module, action, operation_id=operation_id, configuration=configuration,
    )


@pytest.mark.parametrize("path", ["/acquisition/connect", "/engine/modules/processing/start", "/stimulation/start"])
def test_current_action_requires_its_configuration(api_client, path):
    assert api_client.post(f"/api/v1{path}").status_code == 422


def test_start_reading_carries_connection_preconditions(api_client, engine_service):
    identity = {"expected_revision": "source-revision", "generation": "engine-generation", "connection_id": "connection"}
    engine_service.start_reading = AsyncMock()
    response = api_client.post("/api/v1/acquisition/stream/start", json=identity)
    assert response.status_code == 200
    engine_service.start_reading.assert_awaited_once_with(**identity)


def test_start_reading_rejects_missing_connection_preconditions(api_client):
    assert api_client.post("/api/v1/acquisition/stream/start").status_code == 422
