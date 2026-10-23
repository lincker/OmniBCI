"""External nodes obtain one role projection from the settled backend graph."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.plugin_profiles import ProfileBinding, PluginSelection
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.runtime_config import router


def binding(slot, kind, parameters=None):
    return ProfileBinding(slot=slot, kind=kind, profile_id=None,
                          selection=PluginSelection(f"test.{kind}", kind, "1.0.0"),
                          parameters=parameters or {}).to_dict()


@pytest.fixture
def node_api():
    snapshot = {
        "CONFIG.revision": "a" * 64,
        "STIMULATION.auto_spawn": False, "ACTUATION.auto_spawn": False, "ACTUATION.enabled": True,
        "NEUROENGINE.message_server_ip": "127.0.0.1", "NEUROENGINE.message_server_port": 9000,
        "ACTUATION.server_ip": "127.0.0.1", "ACTUATION.server_port": 9001,
        "PLUGINS.bindings": [
            binding("acquisition.source", "acquisition", {"sample_rate": 500, "channel_count": 2,
                                                          "data_file_path": "private.cnt"}),
            binding("stimulation.paradigm", "paradigm"), binding("stimulation.trigger", "trigger"),
            binding("actuation.effector", "effector"),
        ],
    }
    engine = NeuroEngineService(engine_factory=Mock())
    profiles = SimpleNamespace(resolve_settings=lambda: snapshot)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.state.container = SimpleNamespace(node_configuration=NodeConfigurationService(engine, profiles))
    with TestClient(app) as client:
        yield client, engine, snapshot, profiles


@pytest.mark.parametrize("role,kinds", [("stimulation", {"paradigm", "trigger"}), ("actuation", {"effector"})])
def test_node_api_selected_then_running_uses_applied_inputs(node_api, role, kinds):
    client, engine, saved, profiles = node_api
    response = client.get(f"/api/v1/config/nodes/{role}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "selected"
    assert data["configuration"]["generation"] is None
    assert {item["kind"] for item in data["configuration"]["bindings"]} == kinds
    assert "private.cnt" not in response.text
    engine._engine = Mock()
    engine._running = engine._accepting_commands = True
    engine._engine_thread = SimpleNamespace(is_alive=lambda: True)
    engine._applied_snapshot = {**saved, "CONFIG.generation": "run-1"}
    profiles.resolve_settings = Mock(side_effect=AssertionError("running publication read saved data"))
    data = client.get(f"/api/v1/config/nodes/{role}").json()["data"]
    assert data["state"] == "running"
    assert data["configuration"]["generation"] == "run-1"
    assert data["configuration"]["revision"] == "a" * 64


def test_transition_recovery_and_managed_modes_reject_node_publication(node_api):
    client, engine, saved, _ = node_api
    url = "/api/v1/config/nodes/stimulation"
    engine._transitioning = True
    assert client.get(url).status_code == 409
    engine._transitioning = False
    engine.admission.quarantine("uncertain stop")
    assert client.get(url).status_code == 409
    engine.admission.clear_quarantine()
    saved["STIMULATION.auto_spawn"] = True
    assert client.get(url).status_code == 409
    assert client.get("/api/v1/config/nodes/engine").status_code == 422
    client.app.state.container.node_configuration = None
    assert client.get(url).status_code == 503
