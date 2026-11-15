"""Module settings are explicit current values, independently of host-file edits."""
from unittest.mock import Mock

from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS, MODULE_SETTING_DEFAULTS

import pytest

from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.configuration.models import ModuleActivation, EngineSettings
from omnibci.application.configuration.service import ConfigurationService


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
    repository = Mock()
    repository.get_module.return_value = None
    repository.read_server.side_effect = AssertionError("Module edits must not read Server settings")
    service = ConfigurationService(repository, Mock())

    candidate = service.propose("processing", [], settings={"ALGORITHM.baseline_duration": 1})

    assert candidate.settings == {"ALGORITHM.baseline_duration": 1}
    repository.read_server.assert_not_called()
    assert service.propose("processing", [], settings={}).settings == {}


def test_initial_settings_use_owned_defaults_without_reading_storage():
    repository = Mock()
    repository.read_server.side_effect = AssertionError("Module defaults must not read storage")
    service = ConfigurationService(repository, Mock())

    assert service.default_module_settings("processing") == {"ALGORITHM.baseline_duration": 0.0}
    settings = service.default_module_settings("actuation")
    assert settings["ACTUATION.auto_spawn"] is True
    assert settings["ACTUATION.server_port"] == 8001
    assert "NEUROENGINE.queue_size" not in settings
    settings["ACTUATION.server_port"] = 1
    assert service.default_module_settings("actuation")["ACTUATION.server_port"] == 8001
    repository.read_server.assert_not_called()


def test_stimulation_listener_settings_belong_only_to_stimulation():
    settings = {"STIMULATION.server_ip": "display.local", "STIMULATION.server_port": 9000}
    service = ConfigurationService(Mock(), Mock())
    assert service.default_module_settings("stimulation") == MODULE_SETTING_DEFAULTS["stimulation"]
    assert service.propose_engine({}).settings == {}
    assert ModuleActivation("stimulation", settings, ()).settings == settings
    with pytest.raises(PluginProfileValidationError, match="engine section"):
        EngineSettings(settings)


def test_stimulation_listener_default_port_is_8765():
    service = ConfigurationService(Mock(), Mock())
    defaults = service.default_module_settings("stimulation")
    assert defaults["STIMULATION.server_ip"] == "127.0.0.1"
    assert defaults["STIMULATION.server_port"] == 8765


def test_stimulation_listener_change_does_not_change_engine_or_other_module_revisions():
    repository = Mock()
    repository.get_module.return_value = None
    service = ConfigurationService(repository, Mock())
    engine_settings = service.propose_engine(ENGINE_SETTING_DEFAULTS)
    before = {module: ModuleActivation(module, service.default_module_settings(module), ())
              for module in ("acquisition", "processing", "stimulation", "actuation")}
    changed = service.propose("stimulation", [], settings={
        **before["stimulation"].settings, "STIMULATION.server_port": 9000,
    })
    assert changed.revision != before["stimulation"].revision
    assert service.propose_engine(ENGINE_SETTING_DEFAULTS).revision == engine_settings.revision
    for module, previous in before.items():
        assert ModuleActivation(module, service.default_module_settings(module), ()).revision == previous.revision


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
    service = ConfigurationService(repository, Mock())
    integer = service.propose(module, [], settings={key: 1})
    decimal = service.propose(module, [], settings={key: 1.0})
    assert integer.revision == decimal.revision
    assert type(integer.settings[key]) is float
    with pytest.raises(PluginProfileValidationError):
        service.propose(module, [], settings={key: True})
