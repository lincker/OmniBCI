"""Standalone processes use accepted values and never initialize user data."""
from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock

import pytest

from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileBinding
from omnibci.application.services.plugin_config_initialization import SettingsPluginConfigInitializer
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    seed = {
        "ACQUISITION.signal_source": "file_reader", "ACQUISITION.sample_rate": 500,
        "ACQUISITION.channel_number": 10, "ACQUISITION.trigger": "",
        "ALGORITHM.decoder": "THUBCI_SSVEP_classic40CCA",
        "ALGORITHM.epoch_detector": "DEFAULT_EPOCH_DETECTOR", "ALGORITHM.filters": [],
        "STIMULATION.paradigm": "STIMULATION_THUBCI_SSVEP_classic40",
        "ACTUATION.effector": "logger", "ACTUATION.enabled": True,
        "ACTUATION.server_ip": "192.0.2.3", "ACTUATION.server_port": 9010,
    }
    service = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                   CatalogConfigurationAdapter(), settings_source=lambda: seed)
    SettingsPluginConfigInitializer(service).initialize(seed)
    return service


def test_standalone_and_managed_projection_match_after_saved_profile_edit(profiles):
    accepted = profiles.repository.get_activation()
    source = next(binding for binding in accepted.bindings if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    profiles.update(profile.profile_id, name=profile.name,
                    parameters={**profile.parameters, "sample_rate": 250},
                    expected_revision=profile.revision)
    before = {path: path.read_bytes() for path in profiles.repository.root.rglob("*.toml")}

    standalone = accepted_runtime_settings(profiles=profiles, active_kinds={"effector"})

    assert standalone == profiles.materialize(accepted)
    assert standalone["ACQUISITION.sample_rate"] == 500
    assert standalone["CONFIG.revision"] == accepted.revision
    assert standalone["ACTUATION.server_ip"] == "192.0.2.3"
    assert {path: path.read_bytes() for path in profiles.repository.root.rglob("*.toml")} == before


def test_missing_activation_does_not_initialize_storage(tmp_path):
    root = tmp_path / "missing"
    service = PluginProfileService(TomlPluginProfileRepository(root), Mock(), settings_source=lambda: {})
    with pytest.raises(RuntimeError, match="not selected"):
        accepted_runtime_settings(profiles=service)
    with pytest.raises(ValueError, match="not selected"):
        service.resolve_settings()
    assert not root.exists()


def test_corrupt_activation_is_not_replaced(profiles):
    path = profiles.repository.root / "activation.toml"
    path.write_text('revision = "unterminated', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="invalid"):
        accepted_runtime_settings(profiles=profiles)
    assert path.read_bytes() == before


def test_disabled_actuation_does_not_start(profiles):
    accepted = profiles.repository.get_activation()
    disabled = ActivationSnapshot(bindings=accepted.bindings,
                                  settings={**accepted.settings, "ACTUATION.enabled": False})
    profiles.repository.save_activation(disabled, expected_revision=accepted.revision)
    with pytest.raises(RuntimeError, match="disabled"):
        accepted_runtime_settings(profiles=profiles, active_kinds={"effector"})


def test_stimulation_requires_an_accepted_trigger(profiles):
    with pytest.raises(RuntimeError, match="trigger selection"):
        accepted_runtime_settings(profiles=profiles, active_kinds={"paradigm", "trigger"})


def test_missing_selected_version_cannot_use_legacy_alias(profiles):
    accepted = profiles.repository.get_activation()
    source, *others = accepted.bindings
    invalid = replace(source, selection=replace(source.selection, version="missing"))
    profiles.repository.save_activation(
        ActivationSnapshot(bindings=(invalid, *others), settings=accepted.settings),
        expected_revision=accepted.revision,
    )
    with pytest.raises(RuntimeError, match="invalid"):
        accepted_runtime_settings(profiles=profiles)


@pytest.mark.parametrize("kind", ["effector", "paradigm", "trigger"])
def test_unavailable_active_output_is_rejected_before_launch(profiles, monkeypatch, kind):
    accepted = profiles.repository.get_activation()
    if kind == "trigger":
        definition = profiles.catalog.legacy("trigger", "neuroscan_scan45_trigger")
        binding = ProfileBinding("stimulation.trigger", "trigger", None, definition.selection,
                                 {"parallel_port_address": "0x3100"})
        accepted = profiles.repository.save_activation(
            ActivationSnapshot(bindings=(*accepted.bindings, binding), settings=accepted.settings),
            expected_revision=accepted.revision,
        )
    get = profiles.catalog.get

    def unavailable(active_kind, selection):
        definition = get(active_kind, selection)
        return replace(definition, available=False) if active_kind == kind else definition

    monkeypatch.setattr(profiles.catalog, "get", unavailable)
    with pytest.raises(RuntimeError, match="unavailable on this platform"):
        accepted_runtime_settings(profiles=profiles, active_kinds={kind})
    assert profiles.repository.get_activation() == accepted


@pytest.mark.parametrize("role,class_name", [
    ("stimulation", "Stimulation"),
    ("actuation", "Actuation"),
])
def test_scripts_start_backend_configured_supervisor(monkeypatch, role, class_name):
    script = import_module(f"scripts.run_{role}")
    supervisor = Mock()
    application = Mock()
    monkeypatch.setattr(script, "run_configured_node", supervisor)
    monkeypatch.setattr(script, class_name, application)
    script.main(["--backend", "http://192.0.2.1:8000"])
    supervisor.assert_called_once_with(role, application, backend_url="http://192.0.2.1:8000")
    application.assert_not_called()
