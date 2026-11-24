"""Standalone processes use accepted values and never initialize user data."""
from dataclasses import replace
from importlib import import_module
from unittest.mock import Mock

import pytest

from omnibci.application.configuration.models import ActivationSet, ModuleActivation, ProfileBinding
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.configuration.accepted import accepted_runtime_settings
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.plugin_configuration_helpers import accept_profiles, builtin_selection


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    service = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                   CatalogConfigurationAdapter())
    accept_profiles(service, (
        ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 500, "channel_count": 10}),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
        ("actuation.effector", "effector", "logger", {}),
    ), module_settings={"actuation": {"ACTUATION.server_ip": "192.0.2.3", "ACTUATION.server_port": 9010}})
    return service


def test_standalone_and_managed_projection_match_after_saved_profile_edit(profiles):
    accepted = profiles.selected_configuration()
    source = next(binding for binding in accepted.bindings() if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    profiles.update(profile.profile_id, name=profile.name,
                    parameters={**profile.parameters, "sample_rate": 250},
                    expected_revision=profile.revision)
    before = {path: path.read_bytes() for path in profiles.repository.root.rglob("*.toml")}

    standalone = accepted_runtime_settings(profiles=profiles, active_kinds={"effector"})

    assert standalone == profiles.materialize(ActivationSet(
        {"actuation": accepted.modules["actuation"]}, accepted.engine,
    ))
    assert "ACQUISITION.sample_rate" not in standalone
    assert standalone["CONFIG.module_revisions"]["acquisition"] is None
    assert standalone["CONFIG.module_revisions"]["actuation"] == accepted.modules["actuation"].revision
    assert standalone["ACTUATION.server_ip"] == "192.0.2.3"
    assert {path: path.read_bytes() for path in profiles.repository.root.rglob("*.toml")} == before


def test_missing_activation_does_not_initialize_storage(tmp_path):
    root = tmp_path / "missing"
    service = ConfigurationService(TomlConfigurationRepository(root), Mock())
    with pytest.raises(RuntimeError, match="not selected"):
        accepted_runtime_settings(profiles=service)
    assert not root.exists()


def test_corrupt_activation_is_not_replaced(profiles):
    path = profiles.repository.root / "activation" / "acquisition.toml"
    path.write_text('revision = "unterminated', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="invalid"):
        accepted_runtime_settings(profiles=profiles)
    assert path.read_bytes() == before


def test_actuation_requires_its_own_effector_selection(profiles):
    accepted = profiles.selected_configuration()
    current = accepted.modules["actuation"]
    unselected = ModuleActivation("actuation", current.settings, ())
    profiles.repository.save_module(unselected, expected_revision=current.revision)
    with pytest.raises(RuntimeError, match="Missing required selections: actuation.effector"):
        accepted_runtime_settings(profiles=profiles, active_kinds={"effector"})


def test_stimulation_accepts_an_explicit_configuration_without_trigger(profiles):
    accepted = profiles.selected_configuration()
    snapshot = accepted_runtime_settings(profiles=profiles, active_kinds={"paradigm", "trigger"})
    assert snapshot == profiles.materialize(ActivationSet(
        {"stimulation": accepted.modules["stimulation"]}, accepted.engine,
    ))
    assert not any(binding["slot"] == "stimulation.trigger" for binding in snapshot["PLUGINS.bindings"])
    assert profiles.selected_configuration() == accepted


@pytest.mark.parametrize("module,kind", [("stimulation", "paradigm"), ("actuation", "effector")])
def test_output_configuration_ignores_corrupt_acquisition(profiles, module, kind):
    path = profiles.repository.root / "activation" / "acquisition.toml"
    path.write_text('revision = "unterminated', encoding="utf-8")
    snapshot = accepted_runtime_settings(profiles=profiles, active_kinds={kind})
    assert snapshot["CONFIG.module_revisions"][module] is not None
    assert snapshot["CONFIG.module_revisions"]["acquisition"] is None
    assert all(binding["slot"].startswith(module + ".") for binding in snapshot["PLUGINS.bindings"])


def test_missing_selected_version_cannot_use_legacy_alias(profiles):
    accepted = profiles.selected_configuration()
    current = accepted.modules["acquisition"]
    source, = current.bindings
    invalid = replace(source, selection=replace(source.selection, version="99.0.0"))
    profiles.repository.save_module(ModuleActivation("acquisition", current.settings, (invalid,)),
                                    expected_revision=current.revision)
    with pytest.raises(RuntimeError, match="invalid"):
        accepted_runtime_settings(profiles=profiles)


@pytest.mark.parametrize("kind", ["effector", "paradigm", "trigger"])
def test_unavailable_active_output_is_rejected_before_launch(profiles, monkeypatch, kind):
    accepted = profiles.selected_configuration()
    if kind == "trigger":
        selection = builtin_selection("trigger", "neuroscan_scan45_trigger")
        binding = ProfileBinding("stimulation.trigger", "trigger", None, selection,
                                 {"parallel_port_address": "0x3100"})
        current = accepted.modules["stimulation"]
        profiles.repository.save_module(ModuleActivation("stimulation", current.settings, (*current.bindings, binding)),
                                       expected_revision=current.revision)
        accepted = profiles.selected_configuration()
    get = profiles.catalog.get

    def unavailable(active_kind, selection):
        definition = get(active_kind, selection)
        return replace(definition, available=False) if active_kind == kind else definition

    monkeypatch.setattr(profiles.catalog, "get", unavailable)
    with pytest.raises(RuntimeError, match="unavailable on this platform"):
        accepted_runtime_settings(profiles=profiles, active_kinds={kind})
    assert profiles.selected_configuration() == accepted


@pytest.mark.parametrize("role,class_name", [
    ("stimulation", "Stimulation"),
    ("actuation", "Actuation"),
])
def test_scripts_start_server_configured_supervisor(monkeypatch, role, class_name):
    script = import_module(f"scripts.run_{role}")
    supervisor = Mock()
    application = Mock()
    monkeypatch.setattr(script, "run_configured_node", supervisor)
    monkeypatch.setattr(script, class_name, application)
    script.main(["--server", "http://192.0.2.1:8000"])
    supervisor.assert_called_once_with(role, application, server_url="http://192.0.2.1:8000")
    application.assert_not_called()
