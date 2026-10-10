"""Plugin profile HTTP contracts, revision conflicts, and explicit selection."""
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_config import router
from omnibci.presentation.routers.runtime_config import router as runtime_router

PREFIX = "/api/v1/config"


def manual_montage():
    return {"source_id": "file_reader", "sample_rate": 500, "trigger_row_index": 2,
            "channels": [{"index": 0, "name": "C3", "signal_type": "eeg", "unit": "uV"},
                         {"index": 1, "name": "C4", "signal_type": "eeg", "unit": "uV"}],
            "original_reference": "Cz", "provenance": "user-declared"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    state = {"running": False, "settings": {"NEUROENGINE.queue_size": 32}}

    @contextmanager
    def guard(requires_idle):
        if requires_idle and state["running"]:
            raise ConfigProfileConflictError("Stop the engine before selecting configuration")
        yield

    service = PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "configuration"), CatalogConfigurationAdapter(),
        settings_source=lambda: state["settings"], change_guard=guard,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.include_router(runtime_router, prefix="/api/v1")
    engine = SimpleNamespace(is_engine_ready=False, applied_revision=None, generation=None)
    app.state.container = SimpleNamespace(plugin_profile_service=service,
                                         runtime_configuration=RuntimeConfigurationService(service, engine))
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


def accept(http, profiles, expected_revision=None, refresh_slots=None, *, include_host_settings=None):
    body = {"bindings": [{"slot": slot, "profile_id": profile["profile_id"]} for slot, profile in profiles],
            "expected_revision": expected_revision}
    if refresh_slots is not None:
        body["refresh_slots"] = refresh_slots
    if include_host_settings is not None:
        body["include_host_settings"] = include_host_settings
    return http.put(f"{PREFIX}/activation", json=body)


def complete_profiles(client, profiles=()):
    profiles = list(profiles)
    slots = {slot for slot, _ in profiles}
    for slot, kind, alias in (
        ("acquisition.source", "acquisition", "file_reader"),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ):
        if slot not in slots:
            profiles.append((slot, create(client, kind, alias, name=f"Required {kind}")))
    return profiles


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


def test_acquisition_profile_saves_manual_montage_and_pins_it_on_selection(client):
    http, service, _ = client
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": "acquisition", "selection": service.catalog.legacy("acquisition", "file_reader").selection.to_dict(),
        "name": "Manual montage", "parameters": {"sample_rate": 500, "channel_number": 3},
        "manual_montage": manual_montage(),
    })
    assert response.status_code == 201, response.text
    profile = response.json()["data"]
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    assert http.get(path).json()["data"]["manual_montage"]["channels"][0]["name"] == "C3"
    renamed = http.put(path, json={"name": "Renamed", "parameters": profile["parameters"],
                                  "expected_revision": profile["revision"]})
    assert renamed.status_code == 200
    assert renamed.json()["data"]["manual_montage"] == profile["manual_montage"]
    profiles = complete_profiles(client, [("acquisition.source", renamed.json()["data"])])
    selected = accept(http, profiles)
    assert selected.status_code == 200, selected.text
    assert service.resolve_settings()["ACQUISITION.manual_montage"]["channels"][0]["name"] == "C3"
    cleared = http.put(path, json={"name": "Renamed", "parameters": profile["parameters"],
        "expected_revision": renamed.json()["data"]["revision"], "manual_montage": None})
    assert cleared.status_code == 200
    assert cleared.json()["data"]["manual_montage"] is None
    # Saved edits never change already selected acquisition inputs.
    assert service.resolve_settings()["ACQUISITION.manual_montage"]["channels"][0]["name"] == "C3"


def test_read_only_source_observation_is_rejected_by_create_and_update(client):
    http, service, _ = client
    request = {"kind": "acquisition", "selection": service.catalog.legacy("acquisition", "file_reader").selection.to_dict(),
               "name": "Forged observation", "parameters": {}, "acquisition_montage": manual_montage()}
    assert http.post(f"{PREFIX}/plugin-profiles", json=request).status_code == 422
    profile = create(client)
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    assert http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], "acquisition_montage": manual_montage()}).status_code == 422
    assert http.get(path).json()["data"] == profile


@pytest.mark.parametrize("patch", [{"trigger_row_index": 3}, {"sample_rate": 250},
                                 {"source_id": "different-source"}])
def test_manual_montage_requires_consistent_profile_identity_and_dimensions(client, patch):
    http, _, _ = client
    profile = create(client, parameters={"sample_rate": 500, "channel_number": 3})
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    response = http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], "manual_montage": {**manual_montage(), **patch}})
    assert response.status_code == 422, response.text
    assert http.get(path).json()["data"] == profile


@pytest.mark.parametrize("include_host_settings", [False, True])
def test_saving_active_profile_does_not_change_selected_values(client, include_host_settings):
    http, _, state = client
    assert http.get(f"{PREFIX}/activation").json()["data"] is None
    profile = create(client)
    bindings = complete_profiles(client, [("acquisition.source", profile)])
    selected = accept(http, bindings)
    assert selected.status_code == 200, selected.text
    previous = selected.json()["data"]
    state["running"] = True
    saved = http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": "Changed while running", "parameters": {"sample_rate": 500}, "expected_revision": 1,
    })
    assert saved.status_code == 200
    assert http.get(f"{PREFIX}/activation").json()["data"] == previous
    state["settings"]["NEUROENGINE.queue_size"] = 64
    blocked = accept(http, bindings, previous["revision"], include_host_settings=include_host_settings)
    assert blocked.status_code == 409
    assert http.get(f"{PREFIX}/activation").json()["data"] == previous
    assert http.delete(f"{PREFIX}/plugin-profiles/{profile['profile_id']}?expected_revision=2").status_code == 409


@pytest.mark.parametrize("include_host_settings", [None, False, True])
def test_plan_and_direct_selection_use_the_same_candidate(client, include_host_settings):
    http, service, state = client
    source = create(client)
    decoder = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    bindings = complete_profiles(client, [("acquisition.source", source), ("processing.decoder", decoder)])
    before = accept(http, bindings).json()["data"]
    for profile, parameters in ((source, {"sample_rate": 500}), (decoder, {"window_duration": 4.0})):
        assert http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
            "name": profile["name"], "parameters": parameters, "expected_revision": 1,
        }).status_code == 200
    state["settings"]["NEUROENGINE.queue_size"] = 64

    body = {"bindings": [{"slot": slot, "profile_id": profile["profile_id"]} for slot, profile in bindings],
            "expected_revision": before["revision"], "refresh_slots": ["processing.decoder"]}
    if include_host_settings is not None:
        body["include_host_settings"] = include_host_settings
    planned = http.post(f"{PREFIX}/apply-plan", json=body)
    assert planned.status_code == 200, planned.text
    runtime = http.app.state.container.runtime_configuration
    candidate = runtime.resolve_plan(planned.json()["data"]["plan_id"]).snapshot.to_dict()
    assert service.repository.get_activation().to_dict() == before

    response = http.put(f"{PREFIX}/activation", json=body)

    assert response.status_code == 200, response.text
    selected = response.json()["data"]
    assert selected == candidate
    assert selected["revision"] == planned.json()["data"]["target_revision"]
    assert selected["bindings"][0]["parameters"]["sample_rate"] == 1000
    assert selected["bindings"][1]["parameters"]["window_duration"] == 4.0
    assert selected["settings"]["NEUROENGINE.queue_size"] == (64 if include_host_settings else 32)
    assert accept(http, bindings, before["revision"]).status_code == 409


@pytest.mark.parametrize("method, path", [("put", "activation"), ("post", "apply-plan")])
@pytest.mark.parametrize("key, value", [
    ("NEUROENGINE.command_timeout_s", 20),
    ("PERSISTENCE.session_repository", "sqlite"),
    ("PLUGINS.user_dir", "/different/plugin-directory"),
    ("LOGGING.level", "DEBUG"),
    ("LOGGING.file_path", "changed.log"),
    ("CUSTOM.setting", "changed"),
])
def test_startup_and_unknown_host_changes_return_422_without_writes(client, method, path, key, value):
    http, service, state = client
    if key == "CUSTOM.setting":
        state["settings"][key] = "original"
    profiles = complete_profiles(client)
    before = accept(http, profiles).json()["data"]
    stored = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    state["settings"][key] = value

    response = getattr(http, method)(f"{PREFIX}/{path}", json={
        "bindings": [{"slot": slot, "profile_id": profile["profile_id"]} for slot, profile in profiles],
        "expected_revision": before["revision"], "refresh_slots": [], "include_host_settings": True,
    })

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert key in response.json()["message"]
    if key != "CUSTOM.setting":
        assert "restart" in response.json()["message"].lower()
    assert http.get(f"{PREFIX}/activation").json()["data"] == before
    assert {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")} == stored
    assert not http.app.state.container.runtime_configuration._plans


def test_filter_instances_have_stable_slots_and_order(client):
    http, _, _ = client
    profile = create(client, "filter", "BASELINE_CORRECTION")
    first, second = f"processing.filter.{uuid4()}", f"processing.filter.{uuid4()}"
    required = complete_profiles(client)
    before = accept(http, required + [(first, profile), (second, profile)]).json()["data"]
    reordered = accept(http, required + [(second, profile), (first, profile)], before["revision"], [])

    assert reordered.status_code == 200
    assert [item["slot"] for item in reordered.json()["data"]["bindings"]
            if item["kind"] == "filter"] == [second, first]
    assert reordered.json()["data"]["revision"] != before["revision"]
    assert accept(http, required + [(first, profile), (first, profile)], reordered.json()["data"]["revision"]).status_code == 422


def test_empty_activation_returns_422_without_replacing_the_current_selection(client):
    http, _, _ = client
    response = accept(http, complete_profiles(client))
    assert response.status_code == 200, response.text
    before = response.json()["data"]

    rejected = accept(http, [], before["revision"])

    assert rejected.status_code == 422
    assert rejected.json()["code"] == "VALIDATION_ERROR"
    assert http.get(f"{PREFIX}/activation").json()["data"] == before


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
    for name in ("AcceptActivationRequest", "RuntimePlanRequest"):
        assert schemas[name]["properties"]["include_host_settings"]["default"] is False
        assert "include_host_settings" not in schemas[name]["required"]
