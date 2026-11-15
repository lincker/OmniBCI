"""Saving, selecting, and resolving configuration have distinct effects."""
from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.configuration.models import ActivationSet, ModuleActivation, PluginSelection, ProfileBinding, EngineSettings
from omnibci.application.configuration.errors import PluginProfileNotFoundError, ProfileRevisionConflict
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.configuration.service import ConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return ConfigurationService(
        TomlConfigurationRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
    )


def definition(service, kind, alias):
    return next(item for item in service.catalog.list(kind) if item.plugin_type == alias)


def create_source(service, name="Replay", **parameters):
    plugin = definition(service, "acquisition", "file_reader")
    return service.create("acquisition", plugin.selection, name, parameters)


def select(service, profile, slot):
    module = slot.split(".", 1)[0]
    current = service.repository.get_module(module)
    return service.repository.save_module(service.propose(module, [{"slot": slot, "profile_id": profile.profile_id}], settings=service.default_module_settings(module)),
                                 expected_revision=current.revision if current else None)


def runtime(service):
    return service.materialize(service.repository.get_activation_set())


def test_profile_management_does_not_read_unrelated_server_settings(service):
    path = service.repository.root / "settings.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[LOGGING]\nlevel = "unsupported"\n')
    before = path.read_bytes()
    configuration = ConfigurationService(service.repository, service.catalog)

    profile = create_source(configuration, file_reader_chunk_size=123)
    selected = select(configuration, profile, "acquisition.source")

    assert selected.bindings[0].parameters["file_reader_chunk_size"] == 123
    assert path.read_bytes() == before


def test_curry8_parallel_address_survives_profile_save_and_update(service):
    plugin = definition(service, "trigger", "neuroscan_curry8_trigger")
    profile = service.create("trigger", plugin.selection, "CURRY high port", {"parallel_port_address": "0xFFF8"})
    updated = service.update(profile.profile_id, name="Updated CURRY port", parameters=profile.parameters,
                             expected_revision=profile.revision)
    assert updated.parameters == {"parallel_port_address": "0xFFF8"}
    assert updated.revision == profile.revision + 1
    assert all(item is None for item in service.repository.get_activation_set().revisions().values())


def test_decoder_mapping_defaults_initialize_once_and_remain_explicit(service):
    plugin = definition(service, "decoder", "THUBCI_SSVEP_classic40CCA")
    profile = service.create("decoder", plugin.selection, "All channels", {})
    assert profile.processing_input == ProcessingInput()
    updated = service.update(profile.profile_id, name="Still all", parameters=profile.parameters,
                             expected_revision=profile.revision)
    assert updated.processing_input == ProcessingInput()
    assert updated.to_dict()["processing_input"] == {"schema_version": 2, "outputs": []}


def test_manual_decoder_mappings_save_offline_and_remain_independent_after_updates(service):
    plugin = definition(service, "decoder", "THUBCI_SSVEP_classic40CCA")
    old_mapping = ProcessingInput(outputs=(ChannelBinding("Right", source_channel_index=2, signal_type="eeg"),))
    changed_mapping = ProcessingInput(outputs=(ChannelBinding("Left", source_channel_index=0, signal_type="eeg"),))
    profile = service.create("decoder", plugin.selection, "Manual visual", {}, processing_input=old_mapping)
    selected = select(service, profile, "processing.decoder")
    updated = service.update(profile.profile_id, name="Edited", parameters=profile.parameters,
                             processing_input=changed_mapping, expected_revision=profile.revision)
    assert service.repository.get_module("processing") == selected
    assert selected.bindings[0].processing_input == old_mapping
    assert updated.processing_input == changed_mapping
    assert service.profile_history(profile.profile_id)[1].profile.processing_input == old_mapping


def test_save_and_revert_profile_never_change_active_module(service):
    profile = create_source(service, file_reader_chunk_size=100)
    selected = select(service, profile, "acquisition.source")
    updated = service.update(profile.profile_id, name="Changed", parameters={"file_reader_chunk_size": 200},
                             expected_revision=profile.revision)
    restored = service.revert_profile(profile.profile_id, 1, expected_revision=updated.revision)
    assert restored.revision == 3
    assert restored.name == profile.name and restored.parameters == profile.parameters
    assert service.repository.get_module("acquisition") == selected
    assert [item.revision for item in service.profile_history(profile.profile_id)] == [3, 2, 1]
    assert service.repository.list_module_history("acquisition") == []
    with pytest.raises(ProfileRevisionConflict):
        service.revert_profile(profile.profile_id, 1, expected_revision=2)
    with pytest.raises(PluginProfileNotFoundError):
        service.revert_profile(profile.profile_id, 99, expected_revision=3)


def test_profile_reference_refresh_is_explicit(service):
    profile = create_source(service, file_reader_chunk_size=100)
    selected = select(service, profile, "acquisition.source")
    service.update(profile.profile_id, name="New", parameters={"file_reader_chunk_size": 200}, expected_revision=1)
    request = [{"slot": "acquisition.source", "profile_id": profile.profile_id}]
    retained = service.propose("acquisition", request, refresh_slots=[], settings=service.default_module_settings("acquisition"))
    assert retained.bindings == selected.bindings
    refreshed = service.propose("acquisition", request, refresh_slots=["acquisition.source"], settings=service.default_module_settings("acquisition"))
    assert refreshed.bindings[0].profile_revision == 2
    assert refreshed.bindings[0].parameters["file_reader_chunk_size"] == 200
    assert service.repository.get_module("acquisition") == selected


def test_filter_instances_keep_order_and_unique_slots(service):
    plugin = definition(service, "filter", "BASELINE_CORRECTION")
    profile = service.create("filter", plugin.selection, "Baseline", {})
    selections = [{"slot": f"processing.filter.{uuid4()}", "profile_id": profile.profile_id} for _ in range(2)]
    candidate = service.propose("processing", selections, settings=service.default_module_settings("processing"))
    service.repository.save_module(candidate, expected_revision=None)
    reordered = service.propose("processing", list(reversed(selections)), settings=service.default_module_settings("processing"))
    assert reordered.revision != candidate.revision
    assert [item.slot for item in reordered.bindings] == [item["slot"] for item in reversed(selections)]


def test_each_section_keeps_its_explicit_owned_settings(service):
    service.save_server({"LOGGING.level": "DEBUG"}, expected_revision=service.read_server()[1])
    expected = {
        "acquisition": {},
        "processing": {"ALGORITHM.baseline_duration": 2.0},
        "stimulation": {"STIMULATION.auto_spawn": False, "STIMULATION.server_port": 5000},
        "actuation": {"ACTUATION.auto_spawn": False},
    }
    for module, settings in expected.items():
        candidate = service.propose(module, [], settings=settings)
        assert candidate.settings == settings
        service.repository.save_module(candidate, expected_revision=None)
    assert service.repository.get_engine() is None
    engine_settings = service.propose_engine({"NEUROENGINE.queue_size": 64, "debug": True})
    assert engine_settings.settings == {"NEUROENGINE.queue_size": 64, "debug": True}
    service.repository.save_engine(engine_settings, expected_revision=None)
    values = runtime(service)
    assert values["CONFIG.engine_revision"] == engine_settings.revision
    assert set(values["CONFIG.module_revisions"]) == set(expected)
    assert not {"CONFIG.revision", "LOGGING.level", "unknown", "ACQUISITION.sample_rate"} & values.keys()


def test_module_acceptance_preserves_other_modules_and_never_initializes_engine(service):
    processing = service.repository.save_module(service.propose("processing", [], settings=service.default_module_settings("processing")), expected_revision=None)
    source = select(service, create_source(service), "acquisition.source")
    service.propose_engine({"NEUROENGINE.queue_size": 250})
    service.propose("processing", [], settings={"ALGORITHM.baseline_duration": 9.0})
    candidate = service.propose("acquisition", [{"slot": source.bindings[0].slot,
                                               "profile_id": source.bindings[0].profile_id}], settings=service.default_module_settings("acquisition"))
    service.repository.save_module(candidate, expected_revision=source.revision)
    assert candidate.settings == {}
    assert candidate.revision == source.revision
    assert service.repository.get_module("processing") == processing
    assert service.repository.get_engine() is None


@pytest.mark.parametrize("key,value", [
    ("LOGGING.level", "DEBUG"), ("NEUROENGINE.command_timeout_s", 20.0), ("PLUGINS.user_dir", "/new/plugins"),
])
def test_server_startup_edits_do_not_block_engine_candidates(service, key, value):
    service.save_server({key: value}, expected_revision=service.read_server()[1])
    assert service.propose_engine({"NEUROENGINE.queue_size": 32}).settings == {"NEUROENGINE.queue_size": 32}
    assert service.propose("processing", [], settings={}).settings == {}


def test_materialized_runtime_uses_pinned_source_values_and_generation(service):
    profile = create_source(service, file_reader_chunk_size=123)
    source = select(service, profile, "acquisition.source")
    service.update(profile.profile_id, name=profile.name, parameters={"file_reader_chunk_size": 999},
                   expected_revision=profile.revision)
    values = service.materialize(service.repository.get_activation_set(), generation="run-1")
    assert values["ACQUISITION.file_reader_chunk_size"] == 123
    assert values["CONFIG.module_revisions"]["acquisition"] == source.revision
    assert values["CONFIG.generation"] == "run-1"
    assert values["CONFIG.module_revisions"]["processing"] is None
    assert values["CONFIG.engine_revision"] == EngineSettings({}).revision


@pytest.mark.parametrize("kind,slot,alias", [
    ("paradigm", "stimulation.paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("effector", "actuation.effector", "logger"),
])
def test_idle_missing_source_package_does_not_block_foundation_or_outputs(service, monkeypatch, kind, slot, alias):
    source = select(service, create_source(service), "acquisition.source")
    missing = ModuleActivation("acquisition", {}, (
        replace(source.bindings[0], selection=PluginSelection("missing.source", "reader", "1.0.0")),
    ))
    service.repository.save_module(missing, expected_revision=source.revision)
    service.repository.save_engine(service.propose_engine(ENGINE_SETTING_DEFAULTS), expected_revision=None)
    plugin = definition(service, kind, alias)
    output = select(service, service.create(kind, plugin.selection, "Output", {}), slot)
    original_get = service.catalog.get
    monkeypatch.setattr(service.catalog, "get", lambda requested, selection:
                        replace(original_get(requested, selection), available=True))

    foundation = accepted_runtime_settings(profiles=service, active_kinds=set())
    running_output = accepted_runtime_settings(profiles=service, active_kinds={kind})

    assert "ACQUISITION.signal_source" not in foundation
    assert all(revision is None for revision in foundation["CONFIG.module_revisions"].values())
    assert foundation["PLUGINS.bindings"] == []
    assert running_output["CONFIG.module_revisions"]["acquisition"] is None
    assert all(item["slot"].startswith(output.module + ".") for item in running_output["PLUGINS.bindings"])
    assert running_output["CONFIG.module_revisions"][output.module] == output.revision
    assert service.repository.get_module("acquisition") == missing
    with pytest.raises(RuntimeError, match="missing.source"):
        accepted_runtime_settings(profiles=service, active_kinds={"acquisition"})


def external_source_profile(service, monkeypatch, parameters):
    base = definition(service, "acquisition", "file_reader")
    types = {int: "integer", float: "number", bool: "boolean", str: "string"}
    external = replace(base, selection=PluginSelection("test.external-source", "source", "1.0.0"),
        plugin_type="external_source", schema={"type": "object", "additionalProperties": False,
        "properties": {key: {"type": types[type(value)]} for key, value in parameters.items()}})
    original_get = service.catalog.get
    monkeypatch.setattr(service.catalog, "get", lambda kind, selection:
                        external if kind == "acquisition" and selection == external.selection
                        else original_get(kind, selection))
    return service.create("acquisition", external.selection, "External source", parameters)


@pytest.mark.parametrize("key", ["sample_rate", "channel_count"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_declared_source_expectations_must_be_positive_integers(service, monkeypatch, key, value):
    profile = external_source_profile(service, monkeypatch, {key: value})
    candidate = service.propose("acquisition", [{"slot": "acquisition.source", "profile_id": profile.profile_id}], settings=service.default_module_settings("acquisition"))
    with pytest.raises(PluginSchemaError, match="positive integer"):
        service.validate_module(candidate, active_kinds={"acquisition"})
    assert service.materialize(ActivationSet({"acquisition": candidate}, None))[f"ACQUISITION.{key}"] == value
    assert service.repository.get_module("acquisition") is None


def test_external_source_can_declare_one_channel_without_an_event_row(service, monkeypatch):
    profile = external_source_profile(service, monkeypatch, {"sample_rate": 250, "channel_count": 1})
    select(service, profile, "acquisition.source")
    assert runtime(service)["ACQUISITION.sample_rate"] == 250
    assert runtime(service)["ACQUISITION.channel_count"] == 1


@pytest.mark.parametrize("alias", ["file_reader", "neuroscan_curry8_reader"])
def test_observed_sources_can_be_saved_and_applied_before_metadata(service, alias):
    plugin = definition(service, "acquisition", alias)
    profile = service.create("acquisition", plugin.selection, "New source", {})
    assert not {"sample_rate", "channel_count"} & profile.parameters.keys()
    select(service, profile, "acquisition.source")
    assert not {"ACQUISITION.sample_rate", "ACQUISITION.channel_count"} & runtime(service).keys()


def test_curry8_labels_persist_in_profile_and_pinned_runtime(service):
    labels = ["PZ", "PO5", "PO3", "POZ", "PO4", "PO6", "O1", "OZ", "O2", "Trigger"]
    plugin = definition(service, "acquisition", "neuroscan_curry8_reader")
    profile = service.create("acquisition", plugin.selection, "Curry8", {
        "sample_rate": 1000, "channel_count": 10, "channel_labels": labels})
    select(service, profile, "acquisition.source")
    service.update(profile.profile_id, name=profile.name,
                   parameters={**profile.parameters, "channel_labels": ["Changed"] * 10}, expected_revision=1)
    assert runtime(service)["ACQUISITION.channel_labels"] == labels


@pytest.mark.parametrize("channels", [2, 9, 10, 33, 65])
def test_scan45_profile_resolves_its_configured_channel_count(service, channels):
    plugin = definition(service, "acquisition", "neuroscan_scan45_reader")
    profile = service.create("acquisition", plugin.selection, "Device", {"channel_count": channels})
    select(service, profile, "acquisition.source")
    assert runtime(service)["ACQUISITION.channel_count"] == channels


@pytest.mark.parametrize("channels", [True, 0, 1, 3.5, "33"])
def test_scan45_profile_rejects_invalid_channel_count(service, channels):
    plugin = definition(service, "acquisition", "neuroscan_scan45_reader")
    with pytest.raises(PluginSchemaError, match="channel_count"):
        service.create("acquisition", plugin.selection, "Invalid", {"channel_count": channels})


def test_delete_requires_replacing_selected_binding(service):
    profile = create_source(service)
    selected = select(service, profile, "acquisition.source")
    with pytest.raises(ProfileRevisionConflict, match="selected"):
        service.delete(profile.profile_id, expected_revision=1)
    service.repository.save_module(ModuleActivation("acquisition", selected.settings, ()), expected_revision=selected.revision)
    service.delete(profile.profile_id, expected_revision=1)
    assert service.repository.list_profiles() == []


@pytest.mark.parametrize("slot,kind,alias", [
    ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("stimulation.trigger", "trigger", "neuroscan_scan45_trigger"),
    ("actuation.effector", "effector", "logger"),
])
def test_portable_outputs_validate_platform_when_active(service, monkeypatch, slot, kind, alias):
    original_get = service.catalog.get
    monkeypatch.setattr(service.catalog, "get", lambda requested, selection:
                        replace(original_get(requested, selection), available=False))
    plugin = definition(service, kind, alias)
    profile = service.create(kind, plugin.selection, "Portable", {})
    selected = select(service, profile, slot)
    with pytest.raises(PluginSchemaError, match="unavailable"):
        service.validate_module(selected, active_kinds={kind})


def test_profile_schema_unknown_parameters_are_still_rejected(service):
    plugin = definition(service, "acquisition", "file_reader")
    with pytest.raises(PluginSchemaError, match="unknown"):
        service.create("acquisition", plugin.selection, "Invalid", {"unknown": True})


def test_server_restart_does_not_add_server_values_to_runtime(service):
    service.save_server({"NEUROENGINE.command_timeout_s": 20.0},
                        expected_revision=service.read_server()[1])
    previous = service.repository.save_engine(service.propose_engine(ENGINE_SETTING_DEFAULTS), expected_revision=None)
    restarted = ConfigurationService(service.repository, service.catalog)
    assert restarted.propose_engine(previous.settings) == previous
    assert "NEUROENGINE.command_timeout_s" not in restarted.materialize(service.repository.get_activation_set())
