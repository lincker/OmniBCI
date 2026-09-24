"""Plugin profile HTTP contracts, revision conflicts, and explicit selection."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_config import router

PREFIX = "/api/v1/config"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    state = {"running": False, "host": "initial"}

    @contextmanager
    def guard(requires_idle):
        if requires_idle and state["running"]:
            raise ConfigProfileConflictError("Stop the engine before selecting configuration")
        yield

    service = PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "configuration"), CatalogConfigurationAdapter(),
        settings_source=lambda: {"host": state["host"]}, change_guard=guard,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.state.container = SimpleNamespace(plugin_profile_service=service)
    with TestClient(app) as http:
        yield http, service, state


def create(client, kind="acquisition", alias="file_reader", name="Example", parameters=None):
    http, service, _ = client
    selection = service.catalog.legacy(kind, alias).selection.to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": kind, "selection": selection, "name": name, "parameters": parameters or {},
    })
    assert response.status_code == 201, response.text
    return response.json()["data"]


def accept(http, profiles, expected_revision=None, refresh_slots=None):
    body = {"bindings": [{"slot": slot, "profile_id": profile["profile_id"]} for slot, profile in profiles],
            "expected_revision": expected_revision}
    if refresh_slots is not None:
        body["refresh_slots"] = refresh_slots
    return http.put(f"{PREFIX}/activation", json=body)


def test_catalog_exposes_exact_identity_and_plugin_owned_schema(client):
    http, _, _ = client
    response = http.get(f"{PREFIX}/plugin-catalog/acquisition")
    assert response.status_code == 200
    reader = next(item for item in response.json()["data"] if item["plugin_type"] == "file_reader")
    assert reader["selection"]["contribution_id"] == "default"
    assert reader["selection"]["version"]
    assert reader["schema"]["properties"]["file_reader_chunk_size"]["type"] == "integer"
    assert "location" not in reader
    assert http.get(f"{PREFIX}/plugin-catalog/unknown").status_code == 422


def test_profile_crud_and_compare_and_swap(client):
    http, _, _ = client
    profile = create(client)
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    assert http.get(path).json()["data"] == profile
    assert http.get(f"{PREFIX}/plugin-profiles?kind=acquisition").json()["data"] == [profile]
    assert http.get(f"{PREFIX}/plugin-profiles?kind=decoder").json()["data"] == []
    update = {"name": "Renamed", "parameters": {"sample_rate": 500}, "expected_revision": 1}
    saved = http.put(path, json=update)
    assert saved.status_code == 200
    assert saved.json()["data"]["revision"] == 2
    assert http.put(path, json=update).status_code == 409
    assert http.delete(f"{path}?expected_revision=1").status_code == 409
    assert http.delete(f"{path}?expected_revision=2").status_code == 200
    assert http.get(path).status_code == 404


def test_saving_active_profile_does_not_change_selected_values(client):
    http, _, state = client
    assert http.get(f"{PREFIX}/activation").json()["data"] is None
    profile = create(client)
    selected = accept(http, [("acquisition.source", profile)])
    assert selected.status_code == 200, selected.text
    previous = selected.json()["data"]
    state["running"] = True
    saved = http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": "Changed while running", "parameters": {"sample_rate": 500}, "expected_revision": 1,
    })
    assert saved.status_code == 200
    assert http.get(f"{PREFIX}/activation").json()["data"] == previous
    blocked = accept(http, [("acquisition.source", profile)], previous["revision"])
    assert blocked.status_code == 409
    assert http.delete(f"{PREFIX}/plugin-profiles/{profile['profile_id']}?expected_revision=2").status_code == 409


def test_refreshing_one_slot_preserves_other_selected_parameter_copies(client):
    http, _, state = client
    source = create(client)
    decoder = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    bindings = [("acquisition.source", source), ("processing.decoder", decoder)]
    before = accept(http, bindings).json()["data"]
    for profile, parameters in ((source, {"sample_rate": 500}), (decoder, {"window_duration": 4.0})):
        assert http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
            "name": profile["name"], "parameters": parameters, "expected_revision": 1,
        }).status_code == 200
    state["host"] = "current"

    response = accept(http, bindings, before["revision"], ["processing.decoder"])

    assert response.status_code == 200, response.text
    selected = response.json()["data"]
    assert selected["bindings"][0]["parameters"]["sample_rate"] == 1000
    assert selected["bindings"][1]["parameters"]["window_duration"] == 4.0
    assert selected["settings"] == {"host": "current"}
    assert accept(http, bindings, before["revision"]).status_code == 409


def test_filter_instances_have_stable_slots_and_order(client):
    http, _, _ = client
    profile = create(client, "filter", "BASELINE_CORRECTION")
    first, second = f"processing.filter.{uuid4()}", f"processing.filter.{uuid4()}"
    before = accept(http, [(first, profile), (second, profile)]).json()["data"]
    reordered = accept(http, [(second, profile), (first, profile)], before["revision"], [])

    assert reordered.status_code == 200
    assert [item["slot"] for item in reordered.json()["data"]["bindings"]] == [second, first]
    assert reordered.json()["data"]["revision"] != before["revision"]
    assert accept(http, [(first, profile), (first, profile)], reordered.json()["data"]["revision"]).status_code == 422


@pytest.mark.parametrize("parameters", [{"sample_rate": True}, {"sample_rate": 0}, {"unknown": 1}])
def test_invalid_parameters_return_structured_422(client, parameters):
    http, service, _ = client
    selection = service.catalog.legacy("acquisition", "file_reader").selection.to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": "acquisition", "selection": selection, "name": "Bad input", "parameters": parameters,
    })
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_missing_profile_and_unknown_fields_are_rejected(client):
    http, _, _ = client
    assert http.get(f"{PREFIX}/plugin-profiles/{uuid4()}").status_code == 404
    assert http.get(f"{PREFIX}/plugin-profiles/not-a-uuid").status_code == 422
    response = http.put(f"{PREFIX}/activation", json={"bindings": [], "unexpected": True})
    assert response.status_code == 422


def test_openapi_publishes_typed_profile_and_snapshot_contracts(client):
    http, _, _ = client
    schemas = http.get("/openapi.json").json()["components"]["schemas"]
    assert "schema" in schemas["PluginConfigurationResponse"]["properties"]
    assert "expected_revision" in schemas["UpdatePluginProfileRequest"]["required"]
    assert "bindings" in schemas["ActivationResponse"]["required"]
