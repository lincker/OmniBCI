"""Engine configuration and Server diagnostics expose no legacy aliases."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omnibci.presentation.main import create_app
from omnibci.presentation.schemas.config import ConfigEntry, PatchConfigResponse
from omnibci.presentation.schemas.runtime_config import NodeConfigurationResponse, RuntimePlanRequest


@pytest.mark.parametrize("path", [
    "/config/system", "/system/info", "/system/logs", "/backend/info", "/backend/logs",
])
def test_retired_configuration_and_diagnostic_routes_are_absent(path):
    client = TestClient(create_app(api_token=""))
    response = client.get(f"/api/v1{path}")
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_retired_configuration_scope_is_rejected():
    with pytest.raises(ValidationError):
        RuntimePlanRequest(module="system", settings={}, bindings=[])


@pytest.mark.parametrize("version,revision_key", [(2, "engine_revision"), (1, "system_revision")])
def test_retired_node_configuration_shape_is_rejected(version, revision_key):
    with pytest.raises(ValidationError):
        NodeConfigurationResponse(format_version=version, role="stimulation",
                                  module_revisions={"stimulation": "a" * 64},
                                  generation="generation", settings={}, bindings=[],
                                  **{revision_key: "b" * 64})


def test_retired_node_revision_is_rejected_alongside_current_revision():
    configuration = {
        "format_version": 1, "role": "stimulation", "module_revisions": {"stimulation": "a" * 64},
        "engine_revision": "b" * 64, "generation": "generation", "settings": {}, "bindings": [],
    }
    assert NodeConfigurationResponse(**configuration).engine_revision == "b" * 64
    with pytest.raises(ValidationError, match="system_revision"):
        NodeConfigurationResponse(**configuration, system_revision="b" * 64)


def test_retired_restart_field_is_rejected_alongside_current_field():
    with pytest.raises(ValidationError, match="backend_restart_required"):
        PatchConfigResponse(saved=[], revision="a" * 64, server_restart_required=False,
                            backend_restart_required=False)


def test_retired_restart_policy_is_rejected():
    with pytest.raises(ValidationError, match="requires"):
        ConfigEntry(key="LOGGING.level", value="INFO", type="string", requires="backend-restart",
                    description="Logging level", active_value="INFO", restart_required=False)
