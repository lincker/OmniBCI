"""Unit tests for ConfigurationService: profiles, modules, engine, and server settings."""
from dataclasses import replace
import math
import threading
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import uuid4

import pytest

from omnibci.application.configuration.accepted import accepted_runtime_settings
from omnibci.application.configuration.definitions import (
    ENGINE_SETTING_DEFAULTS,
    MODULE_SETTING_DEFAULTS,
    SERVER_STARTUP_DEFAULTS,
)
from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError,
    ConfigProfileConflictError,
    ConfigValidationError,
    PluginProfileNotFoundError,
    PluginProfileValidationError,
    ProfileRevisionConflict,
)
from omnibci.application.configuration.models import (
    ActivationSet,
    EngineSettings,
    ModuleActivation,
    PluginProfile,
)
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.packages import PluginPackageConflictError, PluginPackageError
from omnibci.application.plugin_management.schema import PluginSchemaError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.bootstrap import _RuntimeSettings
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.infrastructure.test_package_store import package_archive
from tests.plugin_configuration_helpers import accept_profiles, builtin_selection


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return ConfigurationService(
        TomlConfigurationRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
    )


def definition(service, kind, alias):
    return service.catalog.get(kind, builtin_selection(kind, alias))


def create_source(service, name="Replay", **parameters):
    plugin = definition(service, "acquisition", "file_reader")
    return service.create("acquisition", plugin.selection, name, parameters)


def select(service, profile, slot):
    module = slot.split(".", 1)[0]
    current = service.repository.get_module(module)
    return service.repository.save_module(
        service.propose(module, [{"slot": slot, "profile_id": profile.profile_id}], settings=service.default_module_settings(module)),
        expected_revision=current.revision if current else None,
    )


def runtime(service):
    return service.materialize(service.selected_configuration())


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
    assert all(item is None for item in service.selected_configuration().revisions().values())


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
    values = service.materialize(service.selected_configuration(), generation="run-1")
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
        schema={"type": "object", "additionalProperties": False,
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
    assert "NEUROENGINE.command_timeout_s" not in restarted.materialize(service.selected_configuration())


@pytest.fixture
def version_services(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    store = ManagedPluginPackageStore()
    lock = RLock()
    profiles = ConfigurationService(
        TomlConfigurationRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
        profile_versions=store, change_guard=lambda: lock,
    )
    for version in ("1.0.0", "2.0.0"):
        store.install_archive(package_archive(tmp_path, version))
    store.set_enabled("acme.device", "1.0.0", True)
    return profiles, store


def version_source_profile(profiles, store):
    selection = store.get_package("acme.device", "1.0.0").contributions[0].selection
    return profiles.create("effector", selection, "Output", {"gain": 2.0})


def test_proposal_and_new_profile_do_not_mutate_accepted_or_source_profile(version_services):
    profiles, store = version_services
    original = version_source_profile(profiles, store)
    snapshot = profiles.propose("actuation", [
        {"slot": "actuation.effector", "profile_id": original.profile_id},
    ], settings=profiles.default_module_settings("actuation"))
    profiles.repository.save_module(snapshot, expected_revision=None)
    proposal = profiles.propose_profile(original.profile_id, "2.0.0", name="Output v2")
    assert proposal.parameters == {"gain": 2.0} and not proposal.target_enabled
    assert len(profiles.repository.list_profiles()) == 1
    with pytest.raises(PluginPackageConflictError, match="Enable"):
        profiles.create_version_profile(original.profile_id, "2.0.0", expected_source_revision=1)
    store.set_enabled("acme.device", "2.0.0", True)
    new = profiles.create_version_profile(original.profile_id, "2.0.0", expected_source_revision=1, name="Output v2")
    assert new.profile_id != original.profile_id and new.selection.version == "2.0.0"
    assert new.parameters == original.parameters
    assert profiles.repository.get_profile(original.profile_id) == original
    assert profiles.repository.get_module("actuation") == snapshot
    rollback = profiles.propose_profile(new.profile_id, "1.0.0")
    assert rollback.selection.version == "1.0.0" and rollback.parameters == original.parameters


def test_profile_copy_rejects_changed_source_revision(version_services):
    profiles, store = version_services
    original = version_source_profile(profiles, store)
    profiles.update(original.profile_id, name="Changed", parameters={"gain": 3.0}, expected_revision=1)
    with pytest.raises(ProfileRevisionConflict, match="preview"):
        profiles.create_version_profile(original.profile_id, "2.0.0", expected_source_revision=1)
    assert len(profiles.repository.list_profiles()) == 1


def test_new_schema_rejects_old_removed_parameters_and_accepts_explicit_replacement(version_services, tmp_path):
    profiles, store = version_services
    original = version_source_profile(profiles, store)
    store.install_archive(package_archive(tmp_path, "3.0.0", properties={"level": {"type": "integer", "default": 5}}, schema_version=2))
    with pytest.raises(PluginSchemaError, match="gain"):
        profiles.propose_profile(original.profile_id, "3.0.0")
    proposal = profiles.propose_profile(original.profile_id, "3.0.0", parameters={"level": 8})
    assert proposal.parameters == {"level": 8} and proposal.configuration_version == 2
    assert profiles.repository.get_profile(original.profile_id).parameters == {"gain": 2.0}


def test_added_target_defaults_are_visible_in_preview(version_services, tmp_path):
    profiles, store = version_services
    original = version_source_profile(profiles, store)
    store.install_archive(package_archive(tmp_path, "3.0.0", properties={
        "gain": {"type": "number", "default": 1.0}, "new_option": {"type": "boolean", "default": True},
    }))
    assert profiles.propose_profile(original.profile_id, "3.0.0").parameters == {"gain": 2.0, "new_option": True}


@pytest.mark.parametrize("channel_input", [ProcessingInput(), ProcessingInput(outputs=(ChannelBinding("O2", "channel-1"), ChannelBinding("O1", "channel-0")))])
def test_decoder_version_copy_preserves_input(version_services, monkeypatch, channel_input):
    profiles, store = version_services
    definition = profiles.catalog.get("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA"))
    source = profiles.create("decoder", definition.selection, "Decoder", {}, processing_input=channel_input)
    target = replace(definition, selection=replace(definition.selection, version="99.0.0"))
    monkeypatch.setattr(store, "get_package", lambda *_: SimpleNamespace(error=None, available=True, enabled=True))
    monkeypatch.setattr(store, "configuration", lambda *_: target)
    monkeypatch.setattr(profiles.catalog, "get", lambda *_: target)

    proposal = profiles.propose_profile(source.profile_id, "99.0.0")
    copied = profiles.create_version_profile(source.profile_id, "99.0.0", expected_source_revision=source.revision)
    assert proposal.processing_input == channel_input
    assert copied.processing_input == channel_input
    assert copied.profile_id != source.profile_id
    assert profiles.repository.get_profile(source.profile_id) == source


@pytest.mark.parametrize("problem", ["package_error", "package_unavailable", "contribution_unavailable", "schema_missing"])
def test_version_proposal_rejects_unavailable_target_without_saving(version_services, monkeypatch, problem):
    profiles, store = version_services
    source = version_source_profile(profiles, store)
    if problem.startswith("package_"):
        package = store.get_package("acme.device", "2.0.0")
        invalid = replace(package, error="Invalid target" if problem == "package_error" else None, available=False)
        monkeypatch.setattr(store, "get_package", lambda *_: invalid)
    else:
        selection = replace(source.selection, version="2.0.0")
        definition = store.configuration(source.kind, selection)
        invalid = (replace(definition, available=False) if problem == "contribution_unavailable"
                   else replace(definition, schema=None))
        monkeypatch.setattr(store, "configuration", lambda *_: invalid)
    with pytest.raises((PluginPackageError, PluginSchemaError)):
        profiles.propose_profile(source.profile_id, "2.0.0")
    assert profiles.repository.list_profiles() == [source]


def test_version_operations_require_explicit_metadata_without_catalog_fallback(service):
    source = create_source(service)
    with pytest.raises(PluginPackageError, match="metadata is unavailable"):
        service.propose_profile(source.profile_id, "2.0.0")
    assert service.repository.list_profiles() == [source]


def test_propose_uses_only_submitted_settings_and_never_reads_host_file():
    repository = Mock()
    repository.get_module.return_value = None
    repository.read_server.side_effect = AssertionError("Module edits must not read Server settings")
    svc = ConfigurationService(repository, Mock())

    candidate = svc.propose("processing", [], settings={"ALGORITHM.baseline_duration": 1})

    assert candidate.settings == {"ALGORITHM.baseline_duration": 1}
    repository.read_server.assert_not_called()
    assert svc.propose("processing", [], settings={}).settings == {}


def test_initial_settings_use_owned_defaults_without_reading_storage():
    repository = Mock()
    repository.read_server.side_effect = AssertionError("Module defaults must not read storage")
    svc = ConfigurationService(repository, Mock())

    assert svc.default_module_settings("processing") == {"ALGORITHM.baseline_duration": 0.0}
    settings = svc.default_module_settings("actuation")
    assert settings["ACTUATION.auto_spawn"] is True
    assert settings["ACTUATION.server_port"] == 8001
    assert "NEUROENGINE.queue_size" not in settings
    settings["ACTUATION.server_port"] = 1
    assert svc.default_module_settings("actuation")["ACTUATION.server_port"] == 8001
    repository.read_server.assert_not_called()


def test_stimulation_listener_settings_belong_only_to_stimulation():
    settings = {"STIMULATION.server_ip": "display.local", "STIMULATION.server_port": 9000}
    svc = ConfigurationService(Mock(), Mock())
    assert svc.default_module_settings("stimulation") == MODULE_SETTING_DEFAULTS["stimulation"]
    assert svc.propose_engine({}).settings == {}
    assert ModuleActivation("stimulation", settings, ()).settings == settings
    with pytest.raises(PluginProfileValidationError, match="engine section"):
        EngineSettings(settings)


def test_stimulation_listener_default_port_is_8765():
    svc = ConfigurationService(Mock(), Mock())
    defaults = svc.default_module_settings("stimulation")
    assert defaults["STIMULATION.server_ip"] == "127.0.0.1"
    assert defaults["STIMULATION.server_port"] == 8765


def test_stimulation_listener_change_does_not_change_engine_or_other_module_revisions():
    repository = Mock()
    repository.get_module.return_value = None
    svc = ConfigurationService(repository, Mock())
    engine_settings = svc.propose_engine(ENGINE_SETTING_DEFAULTS)
    before = {module: ModuleActivation(module, svc.default_module_settings(module), ())
              for module in ("acquisition", "processing", "stimulation", "actuation")}
    changed = svc.propose("stimulation", [], settings={
        **before["stimulation"].settings, "STIMULATION.server_port": 9000,
    })
    assert changed.revision != before["stimulation"].revision
    assert svc.propose_engine(ENGINE_SETTING_DEFAULTS).revision == engine_settings.revision
    for module, previous in before.items():
        assert ModuleActivation(module, svc.default_module_settings(module), ()).revision == previous.revision


@pytest.mark.parametrize("module,key", [
    ("processing", "ALGORITHM.baseline_duration"),
    ("actuation", "ACTUATION.watchdog_timeout_s"),
    ("actuation", "ACTUATION.max_command_age_s"),
])
def test_submitted_numeric_settings_have_one_revision_for_int_and_float(module, key):
    repository = Mock()
    repository.get_module.return_value = None
    svc = ConfigurationService(repository, Mock())
    integer = svc.propose(module, [], settings={key: 1})
    decimal = svc.propose(module, [], settings={key: 1.0})
    assert integer.revision == decimal.revision
    assert type(integer.settings[key]) is float
    with pytest.raises(PluginProfileValidationError):
        svc.propose(module, [], settings={key: True})


def test_engine_candidate_does_not_read_saved_server_values():
    repository = Mock()
    repository.read_server.side_effect = AssertionError("Engine review must not read server settings")
    profiles = ConfigurationService(repository, Mock())
    candidate = profiles.propose_engine({"NEUROENGINE.queue_size": 42, "debug": True})
    assert candidate.settings == {"NEUROENGINE.queue_size": 42, "debug": True}
    repository.read_server.assert_not_called()


def test_engine_review_requires_explicit_current_settings():
    repository = Mock()
    repository.get_engine.return_value = None
    repository.get_module.return_value = None
    profiles = ConfigurationService(repository, Mock())
    engine = NeuroEngineService(lambda snapshot: pytest.fail("Review must not start Engine"))
    rt = RuntimeConfigurationService(profiles, engine)
    with pytest.raises(PluginSchemaError, match="Current Engine settings are required"):
        rt.plan("engine", [], expected_revision=None)
    plan = rt.plan("engine", [], expected_revision=None, settings={"NEUROENGINE.queue_size": 42})
    assert plan.strategy == "select_on_start" and not plan.errors
    assert rt.resolve_plan(plan.plan_id).engine.settings == {"NEUROENGINE.queue_size": 42}
    repository.save_engine.assert_not_called()


def test_engine_timeout_submission_normalizes_numeric_representation():
    profiles = ConfigurationService(Mock(), Mock())
    integer = profiles.propose_engine({"NEUROENGINE.startup_timeout_s": 5})
    decimal = profiles.propose_engine({"NEUROENGINE.startup_timeout_s": 5.0})
    assert integer.revision == decimal.revision
    assert type(integer.settings["NEUROENGINE.startup_timeout_s"]) is float


def test_first_module_selection_uses_engine_defaults_without_reading_server_values():
    repository = Mock()
    repository.get_engine.return_value = None
    repository.get_module.return_value = None
    repository.read_server.side_effect = AssertionError("Module selection must not read server settings")
    profiles = ConfigurationService(repository, Mock())
    engine = NeuroEngineService(lambda snapshot: pytest.fail("Review must not start Engine"))
    rt = RuntimeConfigurationService(profiles, engine)

    plan = rt.plan("processing", [], expected_revision=None,
                   settings={"ALGORITHM.baseline_duration": 1})

    assert rt.resolve_plan(plan.plan_id).engine.settings == ENGINE_SETTING_DEFAULTS
    assert not plan.errors
    repository.read_server.assert_not_called()
    repository.save_engine.assert_not_called()


@pytest.fixture
def server_svc(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[LOGGING]\nlevel="INFO"\nfile_path="omnibci.log"\n'
                    '[NEUROENGINE]\ncommand_timeout_s=10.0\n[PLUGINS]\nuser_dir=""\n')
    return ConfigurationService(TomlConfigurationRepository(tmp_path))


def test_lists_server_settings_and_values_loaded_at_startup(server_svc):
    entries, _ = server_svc.read_server()
    assert {entry["key"]: entry["value"] for entry in entries} == SERVER_STARTUP_DEFAULTS
    assert all(entry["active_value"] == entry["value"] and not entry["restart_required"] for entry in entries)
    assert all(entry["requires"] == "server-restart" for entry in entries)


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", 64), ("NEUROENGINE.startup_timeout_s", 3), ("debug", True),
    ("ACQUISITION.sample_rate", 500), ("ALGORITHM.baseline_duration", 1.0),
    ("STIMULATION.auto_spawn", False), ("ACTUATION.server_port", 8002),
])
def test_engine_and_module_keys_cannot_partially_write_server_settings(server_svc, tmp_path, key, value):
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    with pytest.raises(ConfigKeyNotAllowedError) as raised:
        server_svc.save_server({"LOGGING.level": "DEBUG", key: value}, expected_revision=server_svc.read_server()[1])
    assert raised.value.keys == [key]
    assert path.read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.command_timeout_s", "large"), ("NEUROENGINE.command_timeout_s", True),
    ("NEUROENGINE.command_timeout_s", 0), ("NEUROENGINE.command_timeout_s", -1),
    ("NEUROENGINE.command_timeout_s", math.inf), ("NEUROENGINE.command_timeout_s", math.nan),
    ("LOGGING.level", "verbose"), ("LOGGING.level", False),
    ("LOGGING.file_path", " "), ("LOGGING.file_path", 123), ("PLUGINS.user_dir", False),
])
def test_invalid_server_values_leave_disk_unchanged(server_svc, tmp_path, key, value):
    before = (tmp_path / "settings.toml").read_bytes()
    with pytest.raises(ConfigValidationError) as raised:
        server_svc.save_server({key: value}, expected_revision=server_svc.read_server()[1])
    assert key in raised.value.bad_keys
    assert (tmp_path / "settings.toml").read_bytes() == before


def test_empty_updates_are_rejected(server_svc):
    with pytest.raises(ConfigValidationError):
        server_svc.save_server({}, expected_revision=server_svc.read_server()[1])


def test_saved_values_remain_pending_until_server_restart(server_svc, tmp_path):
    saved, restart, revision = server_svc.save_server({"LOGGING.level": "DEBUG", "NEUROENGINE.command_timeout_s": 20},
                                      expected_revision=server_svc.read_server()[1])
    assert saved == ["LOGGING.level", "NEUROENGINE.command_timeout_s"] and restart
    entries, current_revision = server_svc.read_server()
    assert current_revision == revision
    values = {entry["key"]: entry for entry in entries}
    assert values["LOGGING.level"]["value"] == "DEBUG"
    assert values["LOGGING.level"]["active_value"] == "INFO"
    assert values["LOGGING.level"]["restart_required"]
    restarted = ConfigurationService(TomlConfigurationRepository(tmp_path))
    assert all(not entry["restart_required"] for entry in restarted.read_server()[0])


def test_explicit_startup_snapshot_is_not_replaced_by_file_edits(server_svc):
    active = {**SERVER_STARTUP_DEFAULTS, "LOGGING.level": "WARNING"}
    svc = ConfigurationService(server_svc.repository, active_server_settings=active)
    active["LOGGING.level"] = "ERROR"
    item = next(entry for entry in svc.read_server()[0] if entry["key"] == "LOGGING.level")
    assert item["active_value"] == "WARNING" and item["restart_required"]
    _, restart, _ = svc.save_server({"LOGGING.level": "WARNING"}, expected_revision=svc.read_server()[1])
    assert not restart


def test_server_save_preserves_engine_and_module_configuration(server_svc):
    engine = EngineSettings({"NEUROENGINE.queue_size": 32})
    module = ModuleActivation("stimulation", {"STIMULATION.server_port": 9001}, ())
    server_svc.repository.save_engine(engine, expected_revision=None)
    server_svc.repository.save_module(module, expected_revision=None)

    server_svc.save_server({"LOGGING.level": "DEBUG"}, expected_revision=server_svc.read_server()[1])

    assert server_svc.repository.get_engine().to_dict() == engine.to_dict()
    assert server_svc.repository.get_module("stimulation").to_dict() == module.to_dict()


def test_stale_browser_cannot_overwrite_another_save(server_svc, tmp_path):
    other = ConfigurationService(TomlConfigurationRepository(tmp_path))
    revision = server_svc.read_server()[1]
    _, _, new_revision = other.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    with pytest.raises(ConfigProfileConflictError):
        server_svc.save_server({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert server_svc.read_server()[1] == new_revision
    assert next(entry["value"] for entry in server_svc.read_server()[0] if entry["key"] == "LOGGING.level") == "DEBUG"


def test_storage_failure_keeps_saved_and_active_state(server_svc, tmp_path, monkeypatch):
    before = (tmp_path / "settings.toml").read_bytes()
    original = server_svc.read_server()

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(server_svc.repository, "save_server", fail)
    with pytest.raises(OSError, match="disk full"):
        server_svc.save_server({"LOGGING.level": "DEBUG"}, expected_revision=original[1])
    assert server_svc.read_server() == original
    assert (tmp_path / "settings.toml").read_bytes() == before


@pytest.fixture
def application_config():
    repository = TomlConfigurationRepository()
    repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 32}), expected_revision=None)
    return ConfigurationService(repository)


def test_server_save_does_not_apply_to_running_engine(application_config):
    builder = MagicMock()
    engine = NeuroEngineService(engine_factory=builder)
    engine._running = True
    engine._applied_snapshot = {"NEUROENGINE.queue_size": 32}
    saved, restart, _ = application_config.save_server(
        {"NEUROENGINE.command_timeout_s": 20}, expected_revision=application_config.read_server()[1])
    assert saved == ["NEUROENGINE.command_timeout_s"] and restart
    assert engine.applied_snapshot == {"NEUROENGINE.queue_size": 32}
    assert next(item for item in application_config.read_server()[0]
                if item["key"] == "NEUROENGINE.command_timeout_s")["active_value"] == 10
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_server_save_does_not_wait_for_engine_transition(application_config):
    engine = NeuroEngineService(engine_factory=MagicMock())
    repository = TomlConfigurationRepository(application_config.repository.root)
    before = repository.get_engine()
    async with engine.runtime_transition():
        application_config.save_server({"LOGGING.level": "DEBUG"}, expected_revision=application_config.read_server()[1])
    assert repository.get_engine() == before


def test_server_save_preserves_engine_change_made_during_request(application_config, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    repository = TomlConfigurationRepository(application_config.repository.root)
    before = repository.get_engine()
    update = application_config.repository.save_server
    revision = application_config.read_server()[1]
    errors = []

    def persist(updates, *, expected_revision):
        entered.set()
        assert release.wait(3)
        return update(updates, expected_revision=expected_revision)

    monkeypatch.setattr(application_config.repository, "save_server", persist)

    def save():
        try:
            application_config.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=save)
    thread.start()
    try:
        assert entered.wait(3)
        repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 64}), expected_revision=before.revision)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert repository.get_engine().settings == {"NEUROENGINE.queue_size": 64}
    assert repository.read_server()[0]["LOGGING.level"] == "DEBUG"


@pytest.mark.parametrize("bad", ["selection", "mapping"])
def test_current_configuration_converts_known_input_validation(bad):
    svc = ConfigurationService(Mock(get_module=Mock(return_value=None)))
    svc._parameters = Mock(return_value=(SimpleNamespace(configuration_version=1), {}))
    binding = {"slot": "processing.decoder", "selection": PluginSelection("test.decoder", "main", "1.0.0").to_dict(),
               "parameters": {}, "processing_input": {"schema_version": 2, "outputs": []}}
    if bad == "selection":
        binding["selection"]["version"] = "latest"
    else:
        binding["processing_input"]["outputs"] = [
            {"name": "C3", "source_channel_index": 0}, {"name": "C3", "source_channel_index": 1},
        ]
    with pytest.raises(PluginProfileValidationError):
        svc.propose("processing", [binding], settings={})


def test_stored_selection_failure_uses_declared_configuration_error():
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder", PluginSelection("test.decoder", "main", "1.0.0"),
                            {}, 1, processing_input=ProcessingInput())
    values = profile.to_dict()
    values["selection"]["version"] = "latest"
    with pytest.raises(PluginProfileValidationError):
        PluginProfile.from_dict(values)


def test_profile_version_input_error_is_declared():
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder", PluginSelection("test.decoder", "main", "1.0.0"),
                            {}, 1, processing_input=ProcessingInput())
    svc = ConfigurationService(Mock(get_profile=Mock(return_value=profile)), profile_versions=Mock())
    with pytest.raises(PluginProfileValidationError):
        svc.propose_profile(profile.profile_id, "latest")


def test_unrelated_parameter_implementation_error_is_not_reclassified():
    svc = ConfigurationService(Mock(get_module=Mock(return_value=None)))
    svc._parameters = Mock(side_effect=ValueError("unexpected implementation failure"))
    binding = {"slot": "processing.decoder", "selection": PluginSelection("test.decoder", "main", "1.0.0").to_dict(),
               "parameters": {}, "processing_input": {"schema_version": 2, "outputs": []}}
    with pytest.raises(ValueError, match="unexpected implementation failure") as raised:
        svc.propose("processing", [binding], settings={})
    assert type(raised.value) is ValueError


@pytest.fixture
def consumer_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    svc = ConfigurationService(TomlConfigurationRepository(tmp_path / "configuration"),
                               CatalogConfigurationAdapter())
    accept_profiles(svc, [
        ("acquisition.source", "acquisition", "file_reader", {}),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("actuation.effector", "effector", "logger", {}),
    ], module_settings={"actuation": {"ACTUATION.auto_spawn": False}})
    return svc


def _queries(svc):
    names = ("get_module", "get_engine", "get_profile", "module_history_entry", "materialize",
             "validate_module", "propose", "propose_engine", "default_module_settings")
    return SimpleNamespace(**{name: getattr(svc, name) for name in names},
                           selected_configuration=lambda modules: ActivationSet(
                               {module: svc.get_module(module) for module in modules}, svc.get_engine()),
                           get_plugin_configuration=svc.catalog.get)


def test_selected_query_does_not_read_unrelated_corrupt_module(consumer_profiles):
    path = consumer_profiles.repository.root / "activation" / "processing.toml"
    path.write_text('revision = "unterminated', encoding="utf-8")
    selected = consumer_profiles.selected_configuration(("actuation",))
    assert selected.modules["actuation"] == consumer_profiles.get_module("actuation")
    assert selected.modules["processing"] is None
    assert selected.engine == consumer_profiles.get_engine()


def test_external_node_selected_projection_uses_queries_only(consumer_profiles):
    engine = SimpleNamespace(node_configuration_snapshot=lambda selected, *, module: ("selected", selected()))
    value = NodeConfigurationService(engine, _queries(consumer_profiles)).configuration("actuation")
    assert value["configuration"]["module_revisions"] == {"actuation": consumer_profiles.get_module("actuation").revision}
    assert {binding["kind"] for binding in value["configuration"]["bindings"]} == {"effector"}


def test_startup_selection_uses_queries_without_repository_access(consumer_profiles):
    value = accepted_runtime_settings(profiles=_queries(consumer_profiles), active_kinds={"effector"})
    assert value["CONFIG.module_revisions"]["actuation"] == consumer_profiles.get_module("actuation").revision
    assert value["CONFIG.module_revisions"]["acquisition"] is None


def test_idle_status_reads_configuration_queries(consumer_profiles):
    from omnibci.application.processing_source import source_id_for

    engine = SimpleNamespace(applied_snapshot=None, is_engine_ready=False)
    view = _RuntimeSettings(engine, _queries(consumer_profiles))
    assert source_id_for(view) == "OmniBCI.file_reader/default@0.1.0"


def test_planner_uses_queries_for_revision_and_plugin_apply_hints(consumer_profiles):
    selected = consumer_profiles.selected_configuration()
    engine = NeuroEngineService(lambda _: pytest.fail("Planning must not start the engine"))
    engine._running = engine._accepting_commands = True
    engine._engine = SimpleNamespace()
    engine._engine_thread = SimpleNamespace(is_alive=lambda: True)
    engine._applied_snapshot = consumer_profiles.materialize(selected, "generation")
    engine.is_module_running = lambda module: True
    engine.is_peer_connected = lambda role: False
    svc = RuntimeConfigurationService(_queries(consumer_profiles), engine)
    current = consumer_profiles.get_module("processing")
    decoder = next(binding for binding in current.bindings if binding.kind == "decoder")
    profile = consumer_profiles.get_profile(decoder.profile_id)
    consumer_profiles.update(profile.profile_id, name=profile.name,
                             parameters={**profile.parameters, "window_duration": 1.5}, expected_revision=profile.revision)
    plan = svc.plan("processing", [{"slot": binding.slot, "profile_id": binding.profile_id}
                                   for binding in current.bindings],
                    expected_revision=current.revision, settings=current.settings)
    assert not plan.errors and plan.strategy == "trial_boundary"
    assert svc.resolve_plan(plan.plan_id).plan == plan
    assert svc.status()["accepted"]["processing"] == current.revision


def test_plan_rejects_unknown_strategy_before_submission(consumer_profiles):
    svc = RuntimeConfigurationService(consumer_profiles, NeuroEngineService(lambda _: None))
    plan = svc.plan("actuation", [], expected_revision=consumer_profiles.get_module("actuation").revision,
                    settings=consumer_profiles.default_module_settings("actuation"))
    with pytest.raises(ValueError, match="strategy|ApplyStrategy"):
        replace(plan, strategy="best_effort")
    assert plan.to_dict()["strategy"] == "select_on_start"


@pytest.mark.asyncio
@pytest.mark.parametrize("history_failure", [False, True])
async def test_apply_owns_explicit_repository_without_exposing_it_to_queries(consumer_profiles, monkeypatch, history_failure):
    engine = NeuroEngineService(lambda _: pytest.fail("Selection must not start the engine"))
    surface = _queries(consumer_profiles)
    coordinator = RuntimeApplyCoordinator(engine, surface, None,
        SimpleNamespace(is_active=False, process_alive=False), None, repository=consumer_profiles.repository)
    svc = RuntimeConfigurationService(surface, engine, executor=coordinator)
    current = consumer_profiles.get_module("actuation")
    plan = svc.plan("actuation", [], expected_revision=current.revision,
                    settings=consumer_profiles.default_module_settings("actuation"))
    if history_failure:
        def fail(*args, **kwargs):
            raise OSError("history disk unavailable")
        monkeypatch.setattr(consumer_profiles.repository, "record_module_history", fail)
    operation = svc.submit(plan.plan_id, str(uuid4()))
    await svc.wait_for_idle()
    result = svc.operation(operation["operation_id"])
    assert result["status"] == "succeeded"
    assert result["result"]["outcome"] == "selected"
    assert result["result"]["applied"] is True
    assert result["result"]["history_saved"] is not history_failure
    assert consumer_profiles.get_module("actuation").revision == plan.target_revisions["actuation"]
    if history_failure:
        assert "history disk unavailable" in result["result"]["warning"]
    else:
        assert consumer_profiles.module_history("actuation")[0].entry_id == operation["operation_id"]


def test_signal_setup_uses_queries_for_selected_profiles(consumer_profiles):
    from omnibci.application.services.signal_setup_service import SignalSetupService

    engine = SimpleNamespace(is_engine_ready=False, applied_snapshot=None, admission=None,
                             is_module_running=lambda module: False)
    acquisition = SimpleNamespace(cached_descriptor_for=lambda binding: None)
    svc = SignalSetupService(_queries(consumer_profiles), engine, acquisition, None)
    state = svc.read()
    assert state["selected_source_id"] == "OmniBCI.file_reader/default@0.1.0"
    assert not state["readiness"]["ready"]


def test_returned_plan_revision_edit_does_not_change_the_saved_review(consumer_profiles):
    svc = RuntimeConfigurationService(consumer_profiles, NeuroEngineService(lambda _: None))
    previous = consumer_profiles.get_module("actuation").revision
    plan = svc.plan("actuation", [], expected_revision=previous,
                    settings=consumer_profiles.default_module_settings("actuation"))
    plan.expected_revisions["actuation"] = "caller-edit"
    stored = svc.resolve_plan(plan.plan_id)
    assert stored.plan.expected_revisions["actuation"] == previous


def test_resolved_candidate_edit_cannot_replace_the_pinned_configuration(consumer_profiles):
    svc = RuntimeConfigurationService(consumer_profiles, NeuroEngineService(lambda _: None))
    plan = svc.plan("actuation", [], expected_revision=consumer_profiles.get_module("actuation").revision,
                    settings=consumer_profiles.default_module_settings("actuation"))
    exposed = svc.resolve_plan(plan.plan_id)
    exposed.candidates.clear()
    exposed.plan.target_revisions.clear()
    stored = svc.resolve_plan(plan.plan_id)
    assert stored.candidates["actuation"].revision == plan.target_revisions["actuation"]


def test_foundation_settings_do_not_parse_idle_module_files():
    repository = SimpleNamespace(
        get_engine=Mock(return_value=EngineSettings({})),
        get_module=Mock(side_effect=ValueError("idle module is corrupt")),
    )
    profiles = ConfigurationService(repository)
    profiles.materialize = lambda selected: selected
    selected = accepted_runtime_settings(profiles=profiles, active_kinds=set())
    assert selected.engine is not None
    repository.get_module.assert_not_called()
