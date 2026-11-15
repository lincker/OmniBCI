"""Plugin profile HTTP contracts, revision conflicts, and explicit selection."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from time import monotonic, sleep
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from tests.plugin_configuration_helpers import builtin_selection
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
    state = {"settings": {"NEUROENGINE.queue_size": 32}}
    service = PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "configuration"), CatalogConfigurationAdapter(),
        settings_source=lambda: state["settings"],
    )
    engine = NeuroEngineService(lambda: pytest.fail("Applying stopped configuration must not start the engine"))
    executor = RuntimeApplyCoordinator(engine, service, None,
        SimpleNamespace(is_active=False, process_alive=False), None)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.include_router(runtime_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(plugin_profile_service=service,
        runtime_configuration=RuntimeConfigurationService(service, engine, executor=executor))
    with TestClient(app) as http:
        yield http, service, state


def create(client, kind="acquisition", alias="file_reader", name="Example", parameters=None):
    http, service, _ = client
    selection = builtin_selection(kind, alias).to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": kind, "selection": selection, "name": name, "parameters": parameters or {},
    })
    assert response.status_code == 201, response.text
    return response.json()["data"]


def apply_plan(http, plan):
    assert plan["errors"] == [], plan
    operation_id = str(uuid4())
    response = http.post(f"{PREFIX}/apply", json={"plan_id": plan["plan_id"], "operation_id": operation_id})
    assert response.status_code == 202, response.text
    deadline = monotonic() + 5
    while True:
        result = http.get(f"{PREFIX}/operations/{operation_id}").json()["data"]
        if result["status"] not in ("queued", "applying"):
            assert result["status"] == "succeeded", result
            return result
        assert monotonic() < deadline, result
        sleep(.01)


def accept(http, module, profiles, expected_revision=None, refresh_slots=None):
    response = http.post(f"{PREFIX}/apply-plan", json={
        "module": module, "bindings": [{"slot": slot, "profile_id": profile["profile_id"]} for slot, profile in profiles],
        "expected_revision": expected_revision, "refresh_slots": refresh_slots,
    })
    assert response.status_code == 200, response.text
    apply_plan(http, response.json()["data"])
    return http.get(f"{PREFIX}/modules/{module}").json()["data"]





def test_runtime_plan_accepts_source_draft_without_creating_profile(client):
    http, service, _ = client
    profile = create(client)
    accepted = accept(http, "acquisition", [("acquisition.source", profile)])
    saved = service.repository.list_profiles()
    draft = {"slot": "acquisition.source", "selection": builtin_selection("acquisition", "neuroscan_curry8_reader").to_dict(),
             "parameters": {"DATA_PROVIDER_IP": "192.168.0.20", "DATA_PROVIDER_PORT": 4455}}
    body = {"module": "acquisition", "bindings": [draft], "expected_revision": accepted["revision"],
            "refresh_slots": ["acquisition.source"]}
    response = http.post(f"{PREFIX}/apply-plan", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["errors"] == []
    assert response.json()["data"]["changed_slots"] == ["acquisition.source"]
    assert service.repository.list_profiles() == saved
    assert service.repository.get_module("acquisition").revision == accepted["revision"]
    for invalid in ({**draft, "slot": "processing.decoder"},
                    {**draft, "profile_id": profile["profile_id"]},
                    {**draft, "parameters": {"DATA_PROVIDER_PORT": "4455"}}):
        response = http.post(f"{PREFIX}/apply-plan", json={**body, "bindings": [invalid]})
        assert response.status_code == 422, response.text
    assert service.repository.list_profiles() == saved


@pytest.mark.parametrize("slot,kind,alias", [
    ("acquisition.source", "acquisition", "file_reader"),
    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
    ("processing.filter.00000000-0000-0000-0000-000000000001", "filter", "BASELINE_CORRECTION"),
    ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("stimulation.trigger", "trigger", "neuroscan_scan45_trigger"),
    ("actuation.effector", "effector", "logger"),
])
def test_runtime_plan_accepts_current_values_for_every_plugin_role(client, slot, kind, alias):
    http, service, _ = client
    definition = service.catalog.get(kind, builtin_selection(kind, alias))
    current = {"slot": slot, "selection": definition.selection.to_dict(), "parameters": {}}
    if kind == "decoder":
        current["processing_input"] = {"schema_version": 2, "outputs": [
            {"source_channel_index": 1, "name": "Oz", "signal_type": "eeg"},
        ]}
    response = http.post(f"{PREFIX}/apply-plan", json={
        "module": slot.split(".")[0], "bindings": [current],
    })
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["errors"] == []
    runtime = http.app.state.container.runtime_configuration
    binding, = runtime.resolve_plan(result["plan_id"]).candidates[slot.split(".")[0]].bindings
    assert binding.profile_id is None
    assert binding.kind == kind
    if kind == "decoder":
        assert binding.processing_input.to_dict() == current["processing_input"]
    assert service.repository.list_profiles() == []
    assert all(value is None for value in service.repository.get_activation_set().modules.values())


@pytest.mark.parametrize("changes", [
    {"slot": "processing.unknown"},
    {"slot": "processing.filter.invalid"},
    {"slot": "stimulation.paradigm"},
    {"kind": "decoder"},
    {"configuration_version": True},
    {"configuration_version": 999},
    {"processing_input": None},
    {"processing_input": {"schema_version": 1, "outputs": []}},
    {"processing_input": {"outputs": [{"name": "Missing source"}]}},
    {"processing_input": {"outputs": [
        {"name": "First", "source_channel_index": 0},
        {"name": "Second", "source_channel_index": 0},
    ]}},
])
def test_runtime_plan_rejects_invalid_current_values_without_writes(client, changes):
    http, service, _ = client
    definition = service.catalog.get("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    current = {"slot": "processing.decoder", "selection": definition.selection.to_dict(),
               "parameters": {}, "processing_input": {"schema_version": 2, "outputs": []}, **changes}
    response = http.post(f"{PREFIX}/apply-plan", json={"module": "processing", "bindings": [current]})
    assert response.status_code == 422, response.text
    assert service.repository.list_profiles() == []
    assert all(value is None for value in service.repository.get_activation_set().modules.values())


def test_runtime_plan_requires_explicit_current_decoder_mapping(client):
    http, service, _ = client
    definition = service.catalog.get("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    response = http.post(f"{PREFIX}/apply-plan", json={"module": "processing", "bindings": [{
        "slot": "processing.decoder", "selection": definition.selection.to_dict(), "parameters": {},
    }]})
    assert response.status_code == 422, response.text
    assert "processing_input" in response.json()["message"]
    assert all(value is None for value in service.repository.get_activation_set().modules.values())


def test_acquisition_configuration_does_not_require_processing(client):
    http, service, _ = client
    source = create(client)
    selected = accept(http, "acquisition", [("acquisition.source", source)])
    assert [item["slot"] for item in selected["bindings"]] == ["acquisition.source"]
    assert service.repository.get_module("processing") is None


def test_acquisition_configuration_preserves_other_modules(client):
    http, service, _ = client
    decoder = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    processing = accept(http, "processing", [("processing.decoder", decoder)])
    source = create(client)
    accept(http, "acquisition", [("acquisition.source", source)])
    assert http.get(f"{PREFIX}/modules/processing").json()["data"] == processing


def test_module_configuration_rejects_another_modules_binding(client):
    http, _, _ = client
    decoder = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    response = http.post(f"{PREFIX}/apply-plan", json={"module": "acquisition",
        "bindings": [{"slot": "processing.decoder", "profile_id": decoder["profile_id"]}]})
    assert response.status_code == 422, response.text


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


def test_catalog_exposes_read_only_decoder_channel_requirements(client):
    http, _, _ = client
    response = http.get(f"{PREFIX}/plugin-catalog/decoder")
    assert response.status_code == 200
    decoders = response.json()["data"]
    assert len(decoders) == 2
    for decoder in decoders:
        requirements = decoder["channel_requirements"]
        assert requirements["description"]
        assert requirements["signal_type"] == "eeg"
        assert requirements["min_count"] == 1
        assert requirements["required_names"] == []
        assert requirements["default_names"] == []
    acquisition = http.get(f"{PREFIX}/plugin-catalog/acquisition").json()["data"]
    assert all(plugin["channel_requirements"] is None for plugin in acquisition)


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





@pytest.mark.parametrize("sources", [
    ({"source_channel_id": "channel-1"}, {"source_channel_id": "channel-0"}),
    ({"source_channel_index": 1}, {"source_channel_index": 0}),
])
def test_decoder_profile_saves_and_pins_processing_input(client, sources):
    http, service, _ = client
    profile = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    assert profile["processing_input"] == {"schema_version": 2, "outputs": []}
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    channel_input = {"schema_version": 2, "outputs": [
        {**sources[0], "name": "O2", "signal_type": "eeg"},
        {**sources[1], "name": "O1", "signal_type": "eeg"}]}
    updated = http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], "processing_input": channel_input})
    assert updated.status_code == 200, updated.text
    configured = updated.json()["data"]
    selected = accept(http, "processing", [("processing.decoder", configured)])
    decoder = selected["bindings"][0]
    assert decoder["processing_input"] == configured["processing_input"]
    runtime = service.materialize(service.repository.get_activation_set())
    assert "SIGNAL.setup" not in runtime
    assert next(binding for binding in runtime["PLUGINS.bindings"]
                if binding["slot"] == "processing.decoder")["processing_input"] == configured["processing_input"]
    cleared = http.put(path, json={"name": configured["name"], "parameters": configured["parameters"],
        "expected_revision": configured["revision"], "processing_input": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["data"]["processing_input"] == profile["processing_input"]
    assert service.materialize(service.repository.get_activation_set()) == runtime








def test_new_decoder_with_null_input_uses_explicit_default(client):
    http, service, _ = client
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": "decoder", "selection": builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA").to_dict(),
        "name": "Default input", "processing_input": None,
    })
    assert response.status_code == 201, response.text
    assert response.json()["data"]["processing_input"]["schema_version"] == 2
    assert response.json()["data"]["processing_input"]["outputs"] == []


@pytest.mark.parametrize("channel_input", [
    {"outputs": ["O1", "O1"]}, {"source_id": "file_reader"}, {"original_reference": "Cz"},
    {"assignments": []}, {"reference": {"mode": "average", "channels": []}},
])
def test_invalid_decoder_input_returns_422_without_saving(client, channel_input):
    http, _, _ = client
    profile = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    response = http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], "processing_input": channel_input})
    assert response.status_code == 422, response.text
    assert http.get(path).json()["data"] == profile


def test_acquisition_profile_rejects_processing_input(client):
    http, _, _ = client
    profile = create(client)
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    response = http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], "processing_input": {}})
    assert response.status_code == 422, response.text
    assert http.get(path).json()["data"] == profile


@pytest.mark.parametrize("field, value", [
    ("manual_montage", manual_montage()), ("manual_montage", None),
    ("acquisition_montage", manual_montage()),
])
def test_acquisition_profile_rejects_removed_metadata_fields(client, field, value):
    http, service, _ = client
    request = {"kind": "acquisition", "selection": builtin_selection("acquisition", "file_reader").to_dict(),
               "name": "Source metadata", "parameters": {}, field: value}
    assert http.post(f"{PREFIX}/plugin-profiles", json=request).status_code == 422
    profile = create(client)
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    assert http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], field: value}).status_code == 422
    assert http.get(path).json()["data"] == profile


def test_saving_active_profile_does_not_change_selected_values(client):
    http, service, _ = client
    profile = create(client)
    previous = accept(http, "acquisition", [("acquisition.source", profile)])
    saved = http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": "Changed", "parameters": {"sample_rate": 500}, "expected_revision": 1})
    assert saved.status_code == 200
    assert http.get(f"{PREFIX}/modules/acquisition").json()["data"] == previous
    assert http.delete(f"{PREFIX}/plugin-profiles/{profile['profile_id']}?expected_revision=2").status_code == 409
    assert len(service.repository.list_module_history("acquisition")) == 1


def test_reviewed_candidate_is_applied_and_stale_plan_is_rejected(client):
    http, service, state = client
    profile = create(client, parameters={"sample_rate": 1000, "channel_count": 10})
    before = accept(http, "acquisition", [("acquisition.source", profile)])
    assert http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": profile["name"], "parameters": {"sample_rate": 500}, "expected_revision": 1}).status_code == 200
    state["settings"]["NEUROENGINE.queue_size"] = 64
    body = {"module": "acquisition", "bindings": [{"slot": "acquisition.source", "profile_id": profile["profile_id"]}],
            "expected_revision": before["revision"], "refresh_slots": ["acquisition.source"]}
    planned = http.post(f"{PREFIX}/apply-plan", json=body)
    assert planned.status_code == 200, planned.text
    plan = planned.json()["data"]
    candidate = http.app.state.container.runtime_configuration.resolve_plan(plan["plan_id"]).candidates["acquisition"]
    assert service.repository.get_module("acquisition").revision == before["revision"]
    apply_plan(http, plan)
    selected = http.get(f"{PREFIX}/modules/acquisition").json()["data"]
    assert selected["revision"] == candidate.revision == plan["target_revisions"]["acquisition"]
    assert selected["bindings"][0]["parameters"]["sample_rate"] == 500
    assert "NEUROENGINE.queue_size" not in selected["settings"]
    assert service.repository.get_system().settings["NEUROENGINE.queue_size"] == 32
    assert http.post(f"{PREFIX}/apply-plan", json=body).status_code == 409


@pytest.mark.parametrize("key, value", [
    ("NEUROENGINE.command_timeout_s", 20), ("PLUGINS.user_dir", "/different/plugin-directory"),
    ("LOGGING.level", "DEBUG"), ("LOGGING.file_path", "changed.log"),
])
def test_startup_host_changes_return_422_without_writes(client, key, value):
    http, service, state = client
    stored = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    state["settings"][key] = value
    response = http.post(f"{PREFIX}/apply-plan", json={"module": "system", "bindings": []})
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert key in response.json()["message"]
    assert "restart" in response.json()["message"].lower()
    assert {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")} == stored
    assert not http.app.state.container.runtime_configuration._plans


def test_filter_instances_have_stable_slots_and_order(client):
    http, _, _ = client
    profile = create(client, "filter", "BASELINE_CORRECTION")
    first, second = f"processing.filter.{uuid4()}", f"processing.filter.{uuid4()}"
    before = accept(http, "processing", [(first, profile), (second, profile)])
    reordered = accept(http, "processing", [(second, profile), (first, profile)], before["revision"], [])
    assert [item["slot"] for item in reordered["bindings"]] == [second, first]
    assert reordered["revision"] != before["revision"]
    assert http.post(f"{PREFIX}/apply-plan", json={"module": "processing", "bindings": [
        {"slot": first, "profile_id": profile["profile_id"]}, {"slot": first, "profile_id": profile["profile_id"]}],
        "expected_revision": reordered["revision"]}).status_code == 422


def test_empty_module_can_be_selected_before_startup(client):
    http, _, _ = client
    before = accept(http, "acquisition", [("acquisition.source", create(client))])
    selected = accept(http, "acquisition", [], before["revision"])
    assert selected["bindings"] == []
    assert http.get(f"{PREFIX}/modules/acquisition").json()["data"] == selected


@pytest.mark.parametrize("parameters", [{"sample_rate": True}, {"sample_rate": 0}, {"unknown": 1}])
def test_invalid_parameters_return_structured_422(client, parameters):
    http, service, _ = client
    selection = builtin_selection("acquisition", "file_reader").to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": "acquisition", "selection": selection, "name": "Bad input", "parameters": parameters,
    })
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_missing_profile_and_unknown_fields_are_rejected(client):
    http, _, _ = client
    assert http.get(f"{PREFIX}/plugin-profiles/{uuid4()}").status_code == 404
    assert http.get(f"{PREFIX}/plugin-profiles/not-a-uuid").status_code == 422
    response = http.post(f"{PREFIX}/apply-plan", json={"module": "acquisition", "bindings": [], "unexpected": True})
    assert response.status_code == 422


def test_openapi_publishes_typed_profile_and_module_contracts(client):
    http, _, _ = client
    openapi = http.get("/openapi.json").json()
    schemas = openapi["components"]["schemas"]
    assert "schema" in schemas["PluginConfigurationResponse"]["properties"]
    assert "expected_revision" in schemas["UpdatePluginProfileRequest"]["required"]
    assert "bindings" in schemas["ModuleActivationResponse"]["required"]
    assert "module" in schemas["RuntimePlanRequest"]["required"]
    assert schemas["RuntimePlanRequest"]["properties"]["module"]["enum"] == [
        "acquisition", "processing", "stimulation", "actuation", "system"]
    assert "include_host_settings" not in schemas["RuntimePlanRequest"]["properties"]
    assert f"{PREFIX}/activation" not in openapi["paths"]


def test_unknown_host_fields_do_not_enter_module_or_system_contract(client):
    http, _, state = client
    state["settings"]["CUSTOM.setting"] = "not runtime configuration"
    accept(http, "acquisition", [("acquisition.source", create(client))])
    selected = http.get(f"{PREFIX}/modules").json()["data"]
    assert "CUSTOM.setting" not in selected["system"]["settings"]
    assert all("CUSTOM.setting" not in module["settings"] for module in selected["modules"].values())


def test_profile_history_revert_creates_revision_without_applying(client):
    http, service, _ = client
    profile = create(client, parameters={"sample_rate": 250})
    selected = accept(http, "acquisition", [("acquisition.source", profile)])
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    updated = http.put(path, json={"name": "Second", "parameters": {"sample_rate": 500}, "expected_revision": 1})
    assert updated.status_code == 200
    entries = http.get(path + "/history").json()["data"]
    assert [entry["revision"] for entry in entries] == [2, 1]
    assert entries[-1]["profile"] == profile
    restored = http.post(path + "/revert", json={"revision": 1, "expected_revision": 2})
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["revision"] == 3
    assert restored.json()["data"]["parameters"]["sample_rate"] == 250
    assert http.get(f"{PREFIX}/modules/acquisition").json()["data"] == selected
    assert len(service.repository.list_module_history("acquisition")) == 1
    assert http.post(path + "/revert", json={"revision": 1, "expected_revision": 2}).status_code == 409
    assert http.post(path + "/revert", json={"revision": 99, "expected_revision": 3}).status_code == 404


def test_module_history_records_a_b_a_and_revert_uses_entry_identity(client):
    http, _, _ = client
    profile = create(client, parameters={"sample_rate": 250})
    first = accept(http, "acquisition", [("acquisition.source", profile)])
    entry = http.get(f"{PREFIX}/modules/acquisition/history").json()["data"][0]
    assert http.get(f"{PREFIX}/modules/acquisition/history/{entry['entry_id']}").json()["data"] == first
    updated = http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": "Second", "parameters": {"sample_rate": 500}, "expected_revision": 1})
    assert updated.status_code == 200
    second = accept(http, "acquisition", [("acquisition.source", profile)], first["revision"])
    plan = http.post(f"{PREFIX}/modules/acquisition/revert-plan", json={"entry_id": entry["entry_id"]})
    assert plan.status_code == 200, plan.text
    assert http.get(f"{PREFIX}/modules/acquisition").json()["data"] == second
    operation = apply_plan(http, plan.json()["data"])
    history = http.get(f"{PREFIX}/modules/acquisition/history").json()["data"]
    assert [item["revision"] for item in history] == [first["revision"], second["revision"], first["revision"]]
    assert len({item["entry_id"] for item in history}) == 3
    assert history[0]["entry_id"] == operation["operation_id"]
    assert history[0]["restored_from"] == entry["entry_id"]
    # Applying identical history content is a no-op and creates no history entry.
    plan = http.post(f"{PREFIX}/modules/acquisition/revert-plan", json={"entry_id": entry["entry_id"]})
    assert plan.json()["data"]["strategy"] == "no_change"
    apply_plan(http, plan.json()["data"])
    assert http.get(f"{PREFIX}/modules/acquisition/history").json()["data"] == history
    assert http.get(f"{PREFIX}/modules/acquisition/history/{uuid4()}").status_code == 404
