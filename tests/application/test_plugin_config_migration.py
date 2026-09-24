"""Legacy migration preserves running values and can resume interrupted writes."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.configuration import ConfigProfile
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.plugin_config_migration import (
    LegacyPluginConfigMigration, PluginConfigMigrationError, legacy_parameters,
)
from omnibci.application.services.plugin_config_initialization import SettingsPluginConfigInitializer
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def settings():
    return {
        "ACQUISITION.signal_source": "file_reader", "ACQUISITION.sample_rate": 500,
        "ACQUISITION.channel_number": 10, "ACQUISITION.file_reader_chunk_size": 64,
        "ACQUISITION.data_file_path": "recordings/example.cnt",
        "ACQUISITION.trigger": "neuroscan_scan45_trigger",
        "ACQUISITION.parallel_port_address": "0x3100",
        "ALGORITHM.decoder": "THUBCI_SSVEP_classic40CCA",
        "ALGORITHM.epoch_detector": "DEFAULT_EPOCH_DETECTOR",
        "ALGORITHM.window_duration": 4.0, "ALGORITHM.offset_time": 0.14,
        "ALGORITHM.filters": ["BASELINE_CORRECTION", "BASELINE_CORRECTION"],
        "STIMULATION.paradigm": "STIMULATION_THUBCI_SSVEP_classic40",
        "ACTUATION.effector": "logger", "ACTUATION.enabled": False,
        "ssvep.duration": 2.5, "ssvep.offset_time": 0.2,
        "custom.legacy": {"retain": [1, 2]},
    }


@pytest.fixture
def service(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                CatalogConfigurationAdapter(), settings_source=lambda: settings)


@pytest.fixture(params=["migration", "settings"])
def initialize(service, request):
    if request.param == "settings":
        return SettingsPluginConfigInitializer(service).initialize
    return lambda snapshot: LegacyPluginConfigMigration(service).migrate(snapshot, [], None)


def _legacy(settings, **changes):
    parameters = {key: settings[f"ACQUISITION.{key}"] for key in (
        "sample_rate", "channel_number", "file_reader_chunk_size", "data_file_path",
    )}
    return ConfigProfile(str(uuid4()), "Existing replay", "acquisition", "file_reader",
                         {**parameters, **changes})


def _bindings(result):
    return {binding.slot: binding for binding in result.snapshot.bindings}


def test_migration_preserves_actual_cca_values_ids_and_unrelated_settings(service, settings):
    profile = _legacy(settings)
    original_settings, original_profile = deepcopy(settings), deepcopy(profile)
    result = LegacyPluginConfigMigration(service).migrate(settings, [profile], profile.profile_id)
    bindings = _bindings(result)

    assert not result.already_migrated
    assert bindings["acquisition.source"].profile_id == profile.profile_id
    assert service.repository.get_profile(profile.profile_id).name == profile.name
    assert bindings["processing.decoder"].parameters == {"window_duration": 2.5, "offset_time": 0.2}
    assert bindings["stimulation.trigger"].parameters == {"parallel_port_address": "0x3100"}
    assert any("ssvep.duration" in warning and "ALGORITHM.window_duration" in warning
               for warning in result.warnings)
    assert any("ssvep.offset_time" in warning for warning in result.warnings)
    assert result.snapshot.settings["CONFIG.migration_warnings"] == list(result.warnings)
    assert result.snapshot.settings["custom.legacy"] == settings["custom.legacy"]
    assert settings == original_settings
    assert profile == original_profile


def test_fbcca_uses_algorithm_settings_and_scalar_defaults_only_when_absent(initialize, service, settings):
    settings["ALGORITHM.decoder"] = "THUBCI_SSVEP_classic40FBCCA"
    result = initialize(settings)
    assert _bindings(result)["processing.decoder"].parameters == {
        "window_duration": 4.0, "offset_time": 0.14,
    }
    assert not any("legacy CCA" in warning for warning in result.warnings)
    definition = service.catalog.legacy("decoder", "THUBCI_SSVEP_classic40CCA")
    assert legacy_parameters(definition, {"ALGORITHM.window_duration": 9})["window_duration"] == 3.0
    assert legacy_parameters(definition, {"ssvep.offset_time": 0})["offset_time"] == 0


def test_filter_instances_keep_order_unique_identity_and_idempotence(service, settings):
    migration = LegacyPluginConfigMigration(service)
    result = migration.migrate(settings, [], None)
    filters = [binding for binding in result.snapshot.bindings if binding.kind == "filter"]
    assert len(filters) == 2
    assert filters[0].selection == filters[1].selection
    assert filters[0].slot != filters[1].slot
    assert filters[0].profile_id != filters[1].profile_id
    files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}

    repeated = migration.migrate({}, [], "not-an-existing-id")

    assert repeated.already_migrated
    assert repeated.snapshot == result.snapshot
    assert repeated.warnings == result.warnings
    assert {path: path.read_bytes() for path in files} == files


def test_optional_empty_aliases_are_not_replaced_with_guessed_plugins(initialize, settings):
    settings["ACQUISITION.trigger"] = ""
    settings["ACTUATION.effector"] = ""
    result = initialize(settings)
    assert "stimulation.trigger" not in _bindings(result)
    assert "actuation.effector" not in _bindings(result)


def test_unavailable_platform_preserves_selection_with_warning(initialize, service, settings, monkeypatch):
    original = service.catalog.get

    def get(kind, selection):
        definition = original(kind, selection)
        return replace(definition, available=False) if kind == "trigger" else definition

    monkeypatch.setattr(service.catalog, "get", get)
    result = initialize(settings)
    assert "stimulation.trigger" in _bindings(result)
    assert any("stimulation.trigger" in warning and "unavailable" in warning for warning in result.warnings)


def test_interrupted_profile_writes_resume_without_duplicate_or_revised_profiles(initialize, service, settings, monkeypatch):
    original = service.repository.save_profile
    calls = 0

    def interrupted(profile, expected_revision=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted profile write")
        return original(profile, expected_revision=expected_revision)

    monkeypatch.setattr(service.repository, "save_profile", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        initialize(settings)
    partial = service.repository.list_profiles()
    assert len(partial) == 1
    assert service.repository.get_activation() is None

    monkeypatch.setattr(service.repository, "save_profile", original)
    result = initialize(settings)

    profiles = service.repository.list_profiles()
    assert len(profiles) == len(result.snapshot.bindings)
    assert all(profile.revision == 1 for profile in profiles)
    assert service.repository.get_profile(partial[0].profile_id) == partial[0]


def test_interrupted_activation_write_keeps_durable_profiles_for_retry(initialize, service, settings, monkeypatch):
    original = service.repository.save_activation

    def interrupted(*args, **kwargs):
        raise OSError("interrupted activation write")

    monkeypatch.setattr(service.repository, "save_activation", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        initialize(settings)
    profiles = service.repository.list_profiles()
    assert profiles
    assert service.repository.get_activation() is None
    monkeypatch.setattr(service.repository, "save_activation", original)

    initialize(settings)

    assert service.repository.list_profiles() == profiles


def test_partial_migration_never_overwrites_user_edits(initialize, service, settings, monkeypatch):
    original = service.repository.save_activation
    monkeypatch.setattr(service.repository, "save_activation",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError):
        initialize(settings)
    previous = service.repository.list_profiles()[0]
    edited = service.repository.save_profile(replace(previous, name="User edited"),
                                              expected_revision=previous.revision)
    monkeypatch.setattr(service.repository, "save_activation", original)

    with pytest.raises(ProfileRevisionConflict, match="differs"):
        initialize(settings)
    assert service.repository.get_profile(edited.profile_id) == edited
    assert service.repository.get_activation() is None


@pytest.mark.parametrize("failure", ["missing_plugin", "missing_version", "missing_schema", "missing_alias"])
def test_unresolvable_selected_plugins_leave_activation_untouched(initialize, service, settings, monkeypatch, failure):
    if failure == "missing_plugin":
        settings["ALGORITHM.decoder"] = "unknown_decoder"
    elif failure == "missing_alias":
        del settings["ALGORITHM.decoder"]
    else:
        original = service.catalog.get

        def get(kind, selection):
            definition = original(kind, selection)
            if kind == "decoder":
                if failure == "missing_schema":
                    return replace(definition, schema=None)
                return replace(definition, selection=replace(definition.selection, version=None))
            return definition

        monkeypatch.setattr(service.catalog, "get", get)
    with pytest.raises(PluginConfigMigrationError):
        initialize(settings)
    assert service.repository.get_activation() is None
    assert service.repository.list_profiles() == []


@pytest.mark.parametrize("failure", ["unknown", "missing", "invalid", "whitespace"])
def test_malformed_active_legacy_profile_blocks_migration(service, settings, failure):
    profile = _legacy(settings)
    if failure == "unknown":
        profile.parameters["invented"] = 1
    elif failure == "missing":
        del profile.parameters["sample_rate"]
    elif failure == "invalid":
        profile.parameters["sample_rate"] = True
    else:
        profile.parameters["data_file_path"] = "   "
    with pytest.raises(PluginConfigMigrationError, match="Active acquisition"):
        LegacyPluginConfigMigration(service).migrate(settings, [profile], profile.profile_id)
    assert service.repository.get_activation() is None


def test_invalid_inactive_profile_is_reported_while_valid_uuid_is_preserved(service, settings):
    valid, invalid = _legacy(settings), _legacy(settings, invented=1)
    result = LegacyPluginConfigMigration(service).migrate(settings, [valid, invalid], valid.profile_id)
    assert any(invalid.profile_id in warning and "Skipped inactive" in warning for warning in result.warnings)
    assert service.repository.get_profile(valid.profile_id).parameters == valid.parameters
    assert invalid.profile_id not in {profile.profile_id for profile in service.repository.list_profiles()}


def test_missing_or_inconsistent_active_profile_does_not_create_activation(service, settings):
    migration = LegacyPluginConfigMigration(service)
    with pytest.raises(PluginConfigMigrationError, match="missing"):
        migration.migrate(settings, [], str(uuid4()))
    profile = _legacy(settings, sample_rate=250)
    with pytest.raises(PluginConfigMigrationError, match="disagree"):
        migration.migrate(settings, [profile], profile.profile_id)
    assert service.repository.get_activation() is None
    assert service.repository.list_profiles() == []


def test_existing_settings_initialization_ignores_invalid_inputs(service, settings):
    initializer = SettingsPluginConfigInitializer(service)
    first = initializer.initialize(settings)
    files = {path: path.read_bytes() for path in service.repository.root.rglob("*.toml")}

    repeated = initializer.initialize(
        None, imported_profiles=(object(),), active_acquisition_id="invalid",
    )

    assert repeated.already_migrated
    assert repeated.snapshot == first.snapshot
    assert repeated.warnings == first.warnings
    assert {path: path.read_bytes() for path in files} == files
