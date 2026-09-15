"""Acquisition profile workflows use the same API envelope and lifecycle guard."""
from dataclasses import asdict
from unittest.mock import patch

import pytest

from omnibci.application.services.config_service import ConfigService
from tests.application.test_module_config_service import module_service, create_scan

BASE = "/api/v1/config/modules/acquisition"


@pytest.fixture
def module_client(api_client, module_service, engine_service):
    module_service._change_guard = engine_service.configuration_change
    api_client.app.state.config_service = module_service
    return api_client


def test_create_select_edit_copy_and_delete(module_client, module_service):
    payload = {"plugin_type": "neuroscan_scan45_reader", "name": "Lab A", "parameters": {
        "DATA_PROVIDER_IP": "192.168.0.3", "DATA_PROVIDER_PORT": 4000,
        "sample_rate": 500, "channel_number": 10,
    }}
    result = module_client.post(f"{BASE}/profiles", json=payload)
    assert result.status_code == 201
    profile = result.json()["data"]
    pid = profile["profile_id"]
    assert module_client.put(f"{BASE}/active-profile", json={"profile_id": pid}).status_code == 200
    assert module_client.get(BASE).json()["data"]["effective_parameters"] == payload["parameters"]
    detail = module_client.get(f"{BASE}/profiles/{pid}").json()["data"]
    assert detail == profile
    edited = module_client.put(f"{BASE}/profiles/{pid}", json={
        "name": "Renamed", "parameters": {**payload["parameters"], "sample_rate": 250},
    })
    assert edited.status_code == 200
    assert edited.json()["data"]["profile_id"] == pid
    assert module_service.resolve_snapshot()["ACQUISITION.sample_rate"] == 250
    copied = module_client.post(f"{BASE}/profiles", json={**payload, "name": "Spare"})
    assert copied.status_code == 201 and copied.json()["data"]["profile_id"] != pid
    assert module_client.delete(f"{BASE}/profiles/{pid}").status_code == 409
    assert module_client.put(f"{BASE}/active-profile", json={"profile_id": None}).status_code == 200
    assert module_client.delete(f"{BASE}/profiles/{pid}").status_code == 200
    assert module_client.get(f"{BASE}/profiles/{pid}").status_code == 404


def test_running_engine_allows_inactive_presets_but_rejects_effective_changes(
    module_client, module_service, engine_service,
):
    p = create_scan(module_service)
    module_service.select_module_profile("acquisition", p.profile_id)
    engine_service._running = True
    assert module_client.put(f"{BASE}/active-profile", json={"profile_id": None}).status_code == 409
    assert module_client.put(f"{BASE}/profiles/{p.profile_id}", json={
        "name": "Lab A", "parameters": {**p.parameters, "sample_rate": 250},
    }).status_code == 409
    assert module_client.put(f"{BASE}/profiles/{p.profile_id}", json={
        "name": "Renamed", "parameters": p.parameters,
    }).status_code == 200
    assert module_client.post(f"{BASE}/profiles", json={
        "name": "Spare", "parameters": p.parameters, "plugin_type": p.plugin_type,
    }).status_code == 201
    assert module_client.patch("/api/v1/config", json={
        "updates": {"ACQUISITION.sample_rate": 250},
    }).status_code == 409
    assert module_client.post("/api/v1/config/profiles/deactivate").status_code == 409
    engine_service._running = False


@pytest.mark.parametrize("body", [
    {"name": "invalid", "plugin_type": "unknown", "parameters": {}},
    {"name": "invalid", "plugin_type": "file_reader", "parameters": {"sample_rate": 1000}},
    {"name": "invalid", "plugin_type": "file_reader", "parameters": {}, "profile_id": "client-id"},
])
def test_invalid_creation_is_422_and_writes_nothing(module_client, body):
    result = module_client.post(f"{BASE}/profiles", json=body)
    assert result.status_code == 422 and result.json()["code"] == "VALIDATION_ERROR"
    assert module_client.get(f"{BASE}/profiles").json()["data"]["profiles"] == []


def test_identity_is_not_editable_and_activation_requires_explicit_selection(module_client, module_service):
    p = create_scan(module_service)
    assert module_client.put(f"{BASE}/profiles/{p.profile_id}", json={
        "name": p.name, "parameters": p.parameters, "plugin_type": "file_reader",
    }).status_code == 422
    assert module_client.put(f"{BASE}/active-profile", json={}).status_code == 422
    assert module_client.get("/api/v1/config/modules/montage").status_code == 422


def test_name_collision_and_failed_write_preserve_saved_profile(module_client, module_service):
    p = create_scan(module_service)
    assert module_client.post(f"{BASE}/profiles", json={
        "name": " lab a ", "plugin_type": p.plugin_type, "parameters": p.parameters,
    }).status_code == 409
    with patch("omnibci.infrastructure.repositories.config_profile_repository.os.replace",
               side_effect=PermissionError("Read-only directory")):
        response = module_client.put(f"{BASE}/profiles/{p.profile_id}", json={
            "name": "Renamed", "parameters": p.parameters,
        })
    assert response.status_code == 503
    assert module_client.get(f"{BASE}/profiles/{p.profile_id}").json()["data"] == asdict(p)


def test_missing_selection_is_visible_and_can_be_cleared(module_client, module_service):
    p = create_scan(module_service)
    module_service.select_module_profile("acquisition", p.profile_id)
    module_service._repository.delete("acquisition", p.profile_id)
    detail = module_client.get(BASE)
    assert detail.status_code == 200
    assert detail.json()["data"]["selection_error"]
    assert module_client.put(f"{BASE}/active-profile", json={"profile_id": None}).status_code == 200
    assert module_client.get(BASE).json()["data"]["selection_error"] is None
