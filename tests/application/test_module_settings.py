"""Module settings are explicit current values, independently of host-file edits."""
from unittest.mock import Mock

from omnibci.application.activation_settings import section_settings

import pytest

from omnibci.application.plugin_profiles import ModuleActivation, PluginProfileValidationError, EngineSettings
from omnibci.application.services.plugin_profile_service import PluginProfileService


@pytest.mark.parametrize("module,key,value", [
    ("processing", "ALGORITHM.baseline_duration", -1),
    ("processing", "ALGORITHM.baseline_duration", True),
    ("processing", "ALGORITHM.baseline_duration", "1"),
    ("processing", "ALGORITHM.baseline_duration", float("inf")),
    ("processing", "ALGORITHM.baseline_duration", 10 ** 400),
    ("stimulation", "STIMULATION.renderer", "missing"),
    ("stimulation", "STIMULATION.renderer", []),
    ("stimulation", "STIMULATION.auto_spawn", 1),
    ("actuation", "ACTUATION.auto_spawn", "false"),
    ("actuation", "ACTUATION.server_ip", "  "),
    ("actuation", "ACTUATION.server_ip", 127),
    ("actuation", "ACTUATION.server_port", 0),
    ("actuation", "ACTUATION.server_port", 65536),
    ("actuation", "ACTUATION.server_port", True),
    ("actuation", "ACTUATION.watchdog_timeout_s", 0),
    ("actuation", "ACTUATION.watchdog_timeout_s", float("nan")),
    ("actuation", "ACTUATION.max_command_age_s", -1),
    ("actuation", "ACTUATION.max_command_age_s", False),
    ("acquisition", "ALGORITHM.baseline_duration", 0),
])
def test_invalid_module_setting_is_rejected_before_storage(module, key, value):
    with pytest.raises(PluginProfileValidationError):
        ModuleActivation(module, {key: value}, ())


@pytest.mark.parametrize("module,settings", [
    ("acquisition", {}),
    ("processing", {"ALGORITHM.baseline_duration": 0}),
    ("stimulation", {"STIMULATION.renderer": "pyopengl", "STIMULATION.auto_spawn": False}),
    ("stimulation", {"STIMULATION.renderer": "moderngl"}),
    ("actuation", {"ACTUATION.server_ip": "device.local", "ACTUATION.server_port": 65535,
                   "ACTUATION.watchdog_timeout_s": 0.1, "ACTUATION.max_command_age_s": 0,
                   "ACTUATION.auto_spawn": True}),
])
def test_owned_module_settings_preserve_supported_values(module, settings):
    assert ModuleActivation(module, settings, ()).settings == settings


def test_propose_uses_only_submitted_settings_and_never_reads_host_file():
    source = Mock(return_value={"ALGORITHM.baseline_duration": 7})
    repository = Mock()
    repository.get_module.return_value = None
    service = PluginProfileService(repository, Mock(), settings_source=source)
    source.reset_mock()

    candidate = service.propose("processing", [], settings={"ALGORITHM.baseline_duration": 1})

    assert candidate.settings == {"ALGORITHM.baseline_duration": 1}
    source.assert_not_called()
    assert service.propose("processing", [], settings={}).settings == {}


def test_initial_settings_are_owned_defaults_with_saved_overrides():
    service = PluginProfileService(Mock(), Mock(), settings_source=lambda: {
        "ALGORITHM.baseline_duration": 3, "ACTUATION.auto_spawn": False, "NEUROENGINE.queue_size": 9,
    })
    assert service.default_module_settings("processing") == {"ALGORITHM.baseline_duration": 3}
    settings = service.default_module_settings("actuation")
    assert settings["ACTUATION.auto_spawn"] is False
    assert settings["ACTUATION.server_port"] == 8001
    assert "NEUROENGINE.queue_size" not in settings
    settings["ACTUATION.server_port"] = 1
    assert service.default_module_settings("actuation")["ACTUATION.server_port"] == 8001


def test_stimulation_listener_settings_belong_only_to_stimulation():
    settings = {"STIMULATION.server_ip": "display.local", "STIMULATION.server_port": 9000}
    service = PluginProfileService(Mock(), Mock(), settings_source=lambda: settings)
    assert service.default_module_settings("stimulation") == {
        **settings, "STIMULATION.renderer": "moderngl", "STIMULATION.auto_spawn": True,
    }
    assert service.propose_engine(section_settings(service.settings_source(), "engine")).settings == {}
    assert ModuleActivation("stimulation", settings, ()).settings == settings
    with pytest.raises(PluginProfileValidationError, match="engine section"):
        EngineSettings(settings)


def test_stimulation_listener_default_port_is_8765():
    service = PluginProfileService(Mock(), Mock(), settings_source=lambda: {})
    defaults = service.default_module_settings("stimulation")
    assert defaults["STIMULATION.server_ip"] == "127.0.0.1"
    assert defaults["STIMULATION.server_port"] == 8765


def test_stimulation_listener_change_does_not_change_engine_or_other_module_revisions():
    settings = {"STIMULATION.server_port": 8765, "NEUROENGINE.queue_size": 100}
    service = PluginProfileService(Mock(), Mock(), settings_source=lambda: settings)
    engine_settings = service.propose_engine(section_settings(service.settings_source(), "engine"))
    before = {module: ModuleActivation(module, service.default_module_settings(module), ())
              for module in ("acquisition", "processing", "stimulation", "actuation")}
    settings["STIMULATION.server_port"] = 9000
    assert service.propose_engine(section_settings(service.settings_source(), "engine")).revision == engine_settings.revision
    for module, previous in before.items():
        current = ModuleActivation(module, service.default_module_settings(module), ())
        assert (current.revision != previous.revision) == (module == "stimulation")


@pytest.mark.parametrize("module", ["stimulation", "actuation"])
@pytest.mark.parametrize("field,value", [
    ("server_ip", "  "), ("server_ip", 127),
    ("server_port", 0), ("server_port", 65536), ("server_port", True),
    ("server_port", "8765"), ("server_port", 8765.5),
])
def test_output_transport_values_are_validated_in_the_owning_module(module, field, value):
    key = f"{module.upper()}.{field}"
    with pytest.raises(PluginProfileValidationError, match="Invalid module setting"):
        ModuleActivation(module, {key: value}, ())


@pytest.mark.parametrize("module,key", [
    ("processing", "ALGORITHM.baseline_duration"),
    ("actuation", "ACTUATION.watchdog_timeout_s"),
    ("actuation", "ACTUATION.max_command_age_s"),
])
def test_submitted_numeric_settings_have_one_revision_for_int_and_float(module, key):
    repository = Mock()
    repository.get_module.return_value = None
    service = PluginProfileService(repository, Mock(), settings_source=lambda: {})
    integer = service.propose(module, [], settings={key: 1})
    decimal = service.propose(module, [], settings={key: 1.0})
    assert integer.revision == decimal.revision
    assert type(integer.settings[key]) is float
    with pytest.raises(PluginProfileValidationError):
        service.propose(module, [], settings={key: True})
