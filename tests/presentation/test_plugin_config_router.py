"""Plugin profile HTTP contracts, revision conflicts, and explicit selection."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
import tomlkit
from fastapi import FastAPI
from fastapi.testclient import TestClient

from time import monotonic, sleep
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from tests.plugin_configuration_helpers import builtin_selection
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
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
    service = ConfigurationService(
        TomlConfigurationRepository(tmp_path / "configuration"), CatalogConfigurationAdapter(),
    )
    engine = NeuroEngineService(lambda snapshot: pytest.fail("Applying stopped configuration must not start the engine"))
    executor = RuntimeApplyCoordinator(engine, service, None,
        SimpleNamespace(is_active=False, process_alive=False), None)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1")
    app.include_router(runtime_router, prefix="/api/v1")
    app.state.container = SimpleNamespace(configuration_service=service,
        runtime_configuration=RuntimeConfigurationService(service, engine, executor=executor))
    with TestClient(app) as http:
        yield http, service


def create(client, kind="acquisition", alias="file_reader", name="Example", parameters=None):
    http, service = client
    selection = builtin_selection(kind, alias).to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": kind, "selection": selection, "name": name, "parameters": parameters or {},
    })
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_unavailable_shared_settings_returns_service_error(client, monkeypatch):
    http, service = client

    def unavailable():
        raise OSError("Settings storage unavailable")

    monkeypatch.setattr(service.repository, "get_engine", unavailable)
    response = http.get(f"{PREFIX}/engine")
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


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
    response = http.post(f"{PREFIX}/apply-plan", json={"settings": {},
        "module": module, "bindings": [{"slot": slot, "profile_id": profile["profile_id"]} for slot, profile in profiles],
        "expected_revision": expected_revision, "refresh_slots": refresh_slots,
    })
    assert response.status_code == 200, response.text
    apply_plan(http, response.json()["data"])
    return http.get(f"{PREFIX}/modules/{module}").json()["data"]


def test_absent_module_exposes_owned_defaults_without_creating_configuration(client):
    http, service = client
    response = http.get(f"{PREFIX}/modules/processing")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "module": "processing", "revision": None, "bindings": [],
        "settings": {"ALGORITHM.baseline_duration": 0.0},
    }
    assert service.repository.get_module("processing") is None


def test_accepted_module_read_is_independent_of_invalid_server_values(client):
    http, service = client
    selected = accept(http, "processing", [])
    (service.repository.root / "settings.toml").write_text('[LOGGING]\nlevel = 9\n', encoding="utf-8")
    response = http.get(f"{PREFIX}/modules/processing")
    assert response.status_code == 200
    assert response.json()["data"] == selected





def test_runtime_plan_accepts_source_draft_without_creating_profile(client):
    http, service = client
    profile = create(client)
    accepted = accept(http, "acquisition", [("acquisition.source", profile)])
    saved = service.repository.list_profiles()
    draft = {"slot": "acquisition.source", "selection": builtin_selection("acquisition", "neuroscan_curry8_reader").to_dict(),
             "parameters": {"DATA_PROVIDER_IP": "192.168.0.20", "DATA_PROVIDER_PORT": 4455}}
    body = {"settings": {}, "module": "acquisition", "bindings": [draft], "expected_revision": accepted["revision"],
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
    http, service = client
    definition = service.catalog.get(kind, builtin_selection(kind, alias))
    current = {"slot": slot, "selection": definition.selection.to_dict(), "parameters": {}}
    if kind == "decoder":
        current["processing_input"] = {"schema_version": 2, "outputs": [
            {"source_channel_index": 1, "name": "Oz", "signal_type": "eeg"},
        ]}
    response = http.post(f"{PREFIX}/apply-plan", json={"settings": {},
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
    http, service = client
    definition = service.catalog.get("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    current = {"slot": "processing.decoder", "selection": definition.selection.to_dict(),
               "parameters": {}, "processing_input": {"schema_version": 2, "outputs": []}, **changes}
    response = http.post(f"{PREFIX}/apply-plan", json={"settings": {}, "module": "processing", "bindings": [current]})
    assert response.status_code == 422, response.text
    assert service.repository.list_profiles() == []
    assert all(value is None for value in service.repository.get_activation_set().modules.values())


def test_runtime_plan_requires_explicit_current_decoder_mapping(client):
    http, service = client
    definition = service.catalog.get("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    response = http.post(f"{PREFIX}/apply-plan", json={"settings": {}, "module": "processing", "bindings": [{
        "slot": "processing.decoder", "selection": definition.selection.to_dict(), "parameters": {},
    }]})
    assert response.status_code == 422, response.text
    assert "processing_input" in response.json()["message"]
    assert all(value is None for value in service.repository.get_activation_set().modules.values())


def test_acquisition_configuration_does_not_require_processing(client):
    http, service = client
    source = create(client)
    selected = accept(http, "acquisition", [("acquisition.source", source)])
    assert [item["slot"] for item in selected["bindings"]] == ["acquisition.source"]
    assert service.repository.get_module("processing") is None


def test_acquisition_configuration_preserves_other_modules(client):
    http, service = client
    decoder = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    processing = accept(http, "processing", [("processing.decoder", decoder)])
    source = create(client)
    accept(http, "acquisition", [("acquisition.source", source)])
    assert http.get(f"{PREFIX}/modules/processing").json()["data"] == processing


def test_module_configuration_rejects_another_modules_binding(client):
    http, _ = client
    decoder = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    response = http.post(f"{PREFIX}/apply-plan", json={"settings": {}, "module": "acquisition",
        "bindings": [{"slot": "processing.decoder", "profile_id": decoder["profile_id"]}]})
    assert response.status_code == 422, response.text


def test_catalog_exposes_exact_identity_and_plugin_owned_schema(client):
    http, _ = client
    response = http.get(f"{PREFIX}/plugin-catalog/acquisition")
    assert response.status_code == 200
    reader = next(item for item in response.json()["data"] if item["plugin_type"] == "file_reader")
    assert reader["selection"]["contribution_id"] == "default"
    assert reader["selection"]["version"]
    assert reader["schema"]["properties"]["file_reader_chunk_size"]["type"] == "integer"
    assert "location" not in reader
    assert http.get(f"{PREFIX}/plugin-catalog/unknown").status_code == 422


@pytest.mark.parametrize("kind,alias", [
    ("acquisition", "file_reader"),
    ("decoder", "THUBCI_SSVEP_classic40CCA"),
    ("epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
    ("filter", "BASELINE_CORRECTION"),
    ("paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("trigger", "neuroscan_scan45_trigger"),
    ("effector", "logger"),
])
def test_catalog_lists_each_plugin_kind_without_applying_configuration(client, kind, alias):
    http, service = client
    response = http.get(f"{PREFIX}/plugin-catalog/{kind}")
    assert response.status_code == 200
    plugins = response.json()["data"]
    assert all(plugin["kind"] == kind for plugin in plugins)
    plugin = next(plugin for plugin in plugins if plugin["plugin_type"] == alias)
    assert plugin["selection"] == builtin_selection(kind, alias).to_dict()
    assert plugin["label"]
    assert plugin["schema"]["type"] == "object"
    assert isinstance(plugin["available"], bool)
    assert not {"id", "name", "display_name", "params", "location", "entrypoint"}.intersection(plugin)
    assert service.repository.list_profiles() == []
    assert all(module is None for module in service.repository.get_activation_set().modules.values())


def test_catalog_exposes_stimulation_trial_controls(client):
    http, _ = client
    response = http.get(f"{PREFIX}/plugin-catalog/paradigm")
    assert response.status_code == 200
    paradigm = next(plugin for plugin in response.json()["data"]
                    if plugin["plugin_type"] == "STIMULATION_THUBCI_SSVEP_classic40")
    assert paradigm["controls"]["n_trials"] == {
        "label": "Trial count", "type": "int", "default": 1, "min": 1, "field": "n_trials",
    }
    cue = paradigm["controls"]["target_cue"]
    assert cue["field"] == "trial_params"
    assert cue["default_sequence"] == "1-40"
    assert cue["max"] == 40


def test_catalog_exposes_read_only_decoder_channel_requirements(client):
    http, _ = client
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
    http, _ = client
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
    http, service = client
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
    http, service = client
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
    http, _ = client
    profile = create(client, "decoder", "THUBCI_SSVEP_classic40CCA")
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    response = http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], "processing_input": channel_input})
    assert response.status_code == 422, response.text
    assert http.get(path).json()["data"] == profile


def test_acquisition_profile_rejects_processing_input(client):
    http, _ = client
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
    http, service = client
    request = {"kind": "acquisition", "selection": builtin_selection("acquisition", "file_reader").to_dict(),
               "name": "Source metadata", "parameters": {}, field: value}
    assert http.post(f"{PREFIX}/plugin-profiles", json=request).status_code == 422
    profile = create(client)
    path = f"{PREFIX}/plugin-profiles/{profile['profile_id']}"
    assert http.put(path, json={"name": profile["name"], "parameters": profile["parameters"],
        "expected_revision": profile["revision"], field: value}).status_code == 422
    assert http.get(path).json()["data"] == profile


def test_saving_active_profile_does_not_change_selected_values(client):
    http, service = client
    profile = create(client)
    previous = accept(http, "acquisition", [("acquisition.source", profile)])
    saved = http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": "Changed", "parameters": {"sample_rate": 500}, "expected_revision": 1})
    assert saved.status_code == 200
    assert http.get(f"{PREFIX}/modules/acquisition").json()["data"] == previous
    assert http.delete(f"{PREFIX}/plugin-profiles/{profile['profile_id']}?expected_revision=2").status_code == 409
    assert len(service.repository.list_module_history("acquisition")) == 1


def test_reviewed_candidate_is_applied_and_stale_plan_is_rejected(client):
    http, service = client
    profile = create(client, parameters={"sample_rate": 1000, "channel_count": 10})
    before = accept(http, "acquisition", [("acquisition.source", profile)])
    assert http.put(f"{PREFIX}/plugin-profiles/{profile['profile_id']}", json={
        "name": profile["name"], "parameters": {"sample_rate": 500}, "expected_revision": 1}).status_code == 200
    body = {"settings": {}, "module": "acquisition", "bindings": [{"slot": "acquisition.source", "profile_id": profile["profile_id"]}],
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
    assert service.repository.get_engine().settings["NEUROENGINE.queue_size"] == 100
    assert http.post(f"{PREFIX}/apply-plan", json=body).status_code == 409


@pytest.mark.parametrize("key, value", [
    ("NEUROENGINE.command_timeout_s", 20), ("PLUGINS.user_dir", "/different/plugin-directory"),
    ("LOGGING.level", "DEBUG"), ("LOGGING.file_path", "changed.log"),
])
def test_pending_server_changes_do_not_block_current_engine_review(client, key, value):
    http, service = client
    service.save_server({key: value}, expected_revision=service.read_server()[1])
    stored = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}
    response = http.post(f"{PREFIX}/apply-plan", json={"module": "engine", "bindings": [],
        "settings": {"NEUROENGINE.queue_size": 42}})
    assert response.status_code == 200, response.text
    assert not response.json()["data"]["errors"]
    assert {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")} == stored
    assert http.app.state.container.runtime_configuration._plans


def test_filter_instances_have_stable_slots_and_order(client):
    http, _ = client
    profile = create(client, "filter", "BASELINE_CORRECTION")
    first, second = f"processing.filter.{uuid4()}", f"processing.filter.{uuid4()}"
    before = accept(http, "processing", [(first, profile), (second, profile)])
    reordered = accept(http, "processing", [(second, profile), (first, profile)], before["revision"], [])
    assert [item["slot"] for item in reordered["bindings"]] == [second, first]
    assert reordered["revision"] != before["revision"]
    assert http.post(f"{PREFIX}/apply-plan", json={"settings": {}, "module": "processing", "bindings": [
        {"slot": first, "profile_id": profile["profile_id"]}, {"slot": first, "profile_id": profile["profile_id"]}],
        "expected_revision": reordered["revision"]}).status_code == 422


def test_empty_module_can_be_selected_before_startup(client):
    http, _ = client
    before = accept(http, "acquisition", [("acquisition.source", create(client))])
    selected = accept(http, "acquisition", [], before["revision"])
    assert selected["bindings"] == []
    assert http.get(f"{PREFIX}/modules/acquisition").json()["data"] == selected


@pytest.mark.parametrize("parameters", [{"sample_rate": True}, {"sample_rate": 0}, {"unknown": 1}])
def test_invalid_parameters_return_structured_422(client, parameters):
    http, service = client
    selection = builtin_selection("acquisition", "file_reader").to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": "acquisition", "selection": selection, "name": "Bad input", "parameters": parameters,
    })
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_missing_profile_and_unknown_fields_are_rejected(client):
    http, _ = client
    assert http.get(f"{PREFIX}/plugin-profiles/{uuid4()}").status_code == 404
    assert http.get(f"{PREFIX}/plugin-profiles/not-a-uuid").status_code == 422
    response = http.post(f"{PREFIX}/apply-plan", json={"settings": {}, "module": "acquisition", "bindings": [], "unexpected": True})
    assert response.status_code == 422


@pytest.mark.parametrize(("field", "value"), [
    ("package_id", " acme.test"), ("contribution_id", ""), ("version", "1.0.0."),
])
def test_invalid_identity_returns_validation_error_without_saving(client, field, value):
    http, service = client
    selection = builtin_selection("acquisition", "file_reader").to_dict()
    response = http.post(f"{PREFIX}/plugin-profiles", json={
        "kind": "acquisition", "selection": {**selection, field: value},
        "name": "Invalid selection", "parameters": {},
    })
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert service.repository.list_profiles() == []


def test_invalid_stored_identity_remains_an_http_validation_error(client):
    http, service = client
    profile = create(client)
    path = service.repository.root / "plugin-profiles" / profile["profile_id"] / "current.toml"
    document = tomlkit.parse(path.read_text())
    document["selection"]["version"] = "1.0.0."
    path.write_text(tomlkit.dumps(document))
    for route in (f"{PREFIX}/plugin-profiles/{profile['profile_id']}", f"{PREFIX}/plugin-profiles"):
        response = http.get(route)
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"


def test_configuration_queries_need_no_repository_or_catalog_access(client):
    http, service = client
    profile = create(client, kind="effector", alias="logger")
    activation = service.repository.save_module(service.propose(
        "actuation", [{"slot": "actuation.effector", "profile_id": profile["profile_id"]}],
        settings=service.default_module_settings("actuation"),
    ))
    history = service.repository.record_module_history(activation, str(uuid4()))
    engine = service.repository.save_engine(service.propose_engine({"debug": False}))
    queries = SimpleNamespace(
        list_plugin_configurations=service.catalog.list,
        list_profiles=service.repository.list_profiles, get_profile=service.repository.get_profile,
        get_module=service.repository.get_module, get_engine=service.repository.get_engine,
        module_history=service.repository.list_module_history,
        module_history_entry=service.repository.get_module_history_entry,
        default_module_settings=service.default_module_settings,
    )
    http.app.state.container.configuration_service = queries

    catalog = http.get(f"{PREFIX}/plugin-catalog/effector")
    assert catalog.status_code == 200 and catalog.json()["data"]
    assert http.get(f"{PREFIX}/plugin-profiles?kind=effector").json()["data"][0]["profile_id"] == profile["profile_id"]
    assert http.get(f"{PREFIX}/plugin-profiles/{profile['profile_id']}").json()["data"]["parameters"] == profile["parameters"]
    assert http.get(f"{PREFIX}/modules/actuation").json()["data"]["revision"] == activation.revision
    assert http.get(f"{PREFIX}/modules").json()["data"]["engine"]["revision"] == engine.revision
    assert http.get(f"{PREFIX}/engine").json()["data"]["revision"] == engine.revision
    assert http.get(f"{PREFIX}/modules/actuation/history").json()["data"][0]["entry_id"] == history.entry_id
    assert http.get(f"{PREFIX}/modules/actuation/history/{history.entry_id}").json()["data"]["revision"] == activation.revision
    assert http.get(f"{PREFIX}/modules/actuation/history/{uuid4()}").status_code == 404


def test_module_overview_keeps_partial_query_errors(client):
    http, service = client
    def get_module(module):
        if module == "processing":
            raise ValueError("Processing configuration unreadable")
        return None
    def get_engine():
        raise OSError("Engine settings unreadable")
    http.app.state.container.configuration_service = SimpleNamespace(
        get_module=get_module, get_engine=get_engine,
        default_module_settings=service.default_module_settings,
    )
    response = http.get(f"{PREFIX}/modules")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["errors"] == {"processing": "Processing configuration unreadable", "engine": "Engine settings unreadable"}
    assert data["engine"] is None
    assert data["modules"]["actuation"]["bindings"] == []


def test_openapi_publishes_typed_profile_and_module_contracts(client):
    http, _ = client
    openapi = http.get("/openapi.json").json()
    schemas = openapi["components"]["schemas"]
    assert "schema" in schemas["PluginConfigurationResponse"]["properties"]
    assert "expected_revision" in schemas["UpdatePluginProfileRequest"]["required"]
    assert "bindings" in schemas["ModuleActivationResponse"]["required"]
    assert "module" in schemas["RuntimePlanRequest"]["required"]
    assert schemas["RuntimePlanRequest"]["properties"]["module"]["enum"] == [
        "acquisition", "processing", "stimulation", "actuation", "engine"]
    assert "include_host_settings" not in schemas["RuntimePlanRequest"]["properties"]
    assert f"{PREFIX}/activation" not in openapi["paths"]


@pytest.mark.parametrize("module", ["acquisition", "engine"])
def test_unknown_settings_are_rejected_by_module_and_engine(client, module):
    http, service = client
    response = http.post(f"{PREFIX}/apply-plan", json={
        "module": module, "bindings": [], "settings": {"CUSTOM.setting": "unknown"},
    })
    assert response.status_code == 422, response.text
    assert service.repository.get_engine() is None
    assert service.repository.get_module("acquisition") is None


def test_profile_history_revert_creates_revision_without_applying(client):
    http, service = client
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
    http, _ = client
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
