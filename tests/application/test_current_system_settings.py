"""System review uses submitted values independently of backend startup edits."""
from unittest.mock import Mock

import pytest

from omnibci.application.activation_settings import SYSTEM_SETTING_DEFAULTS
from omnibci.application.plugin_profiles import PluginProfileValidationError, SystemSettings
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", True), ("NEUROENGINE.queue_size", 0),
    ("NEUROENGINE.queue_size", 1.5), ("NEUROENGINE.queue_size", "100"),
    ("NEUROENGINE.startup_timeout_s", False), ("NEUROENGINE.startup_timeout_s", 0),
    ("NEUROENGINE.startup_timeout_s", -1), ("NEUROENGINE.startup_timeout_s", float("inf")),
    ("NEUROENGINE.startup_timeout_s", 10 ** 400), ("debug", 1), ("debug", "false"),
])
def test_invalid_system_values_cannot_be_accepted(key, value):
    with pytest.raises(PluginProfileValidationError):
        SystemSettings({key: value})


def test_system_candidate_does_not_read_saved_backend_values():
    source = Mock(side_effect=AssertionError("System review must not read backend settings"))
    profiles = PluginProfileService(Mock(), Mock(), settings_source=source)
    candidate = profiles.propose_system({"NEUROENGINE.queue_size": 42, "debug": True})
    assert candidate.settings == {"NEUROENGINE.queue_size": 42, "debug": True}
    source.assert_not_called()


def test_system_review_requires_explicit_current_settings():
    repository = Mock()
    repository.get_system.return_value = None
    repository.get_module.return_value = None
    profiles = PluginProfileService(repository, Mock(), settings_source=lambda: {})
    engine = NeuroEngineService(lambda snapshot: pytest.fail("Review must not start Engine"))
    runtime = RuntimeConfigurationService(profiles, engine)
    with pytest.raises(PluginSchemaError, match="Current system settings are required"):
        runtime.plan("system", [], expected_revision=None)
    plan = runtime.plan("system", [], expected_revision=None, settings={"NEUROENGINE.queue_size": 42})
    assert plan.strategy == "select_on_start" and not plan.errors
    assert runtime.resolve_plan(plan.plan_id).system.settings == {"NEUROENGINE.queue_size": 42}
    repository.save_system.assert_not_called()


def test_system_timeout_submission_normalizes_numeric_representation():
    profiles = PluginProfileService(Mock(), Mock(), settings_source=lambda: {})
    integer = profiles.propose_system({"NEUROENGINE.startup_timeout_s": 5})
    decimal = profiles.propose_system({"NEUROENGINE.startup_timeout_s": 5.0})
    assert integer.revision == decimal.revision
    assert type(integer.settings["NEUROENGINE.startup_timeout_s"]) is float


def test_first_module_selection_uses_engine_defaults_without_reading_backend_values():
    repository = Mock()
    repository.get_system.return_value = None
    repository.get_module.return_value = None
    source = Mock(side_effect=AssertionError("Module selection must not read backend settings"))
    profiles = PluginProfileService(repository, Mock(), settings_source=source)
    engine = NeuroEngineService(lambda snapshot: pytest.fail("Review must not start Engine"))
    runtime = RuntimeConfigurationService(profiles, engine)

    plan = runtime.plan("processing", [], expected_revision=None,
                        settings={"ALGORITHM.baseline_duration": 1})

    assert runtime.resolve_plan(plan.plan_id).system.settings == SYSTEM_SETTING_DEFAULTS
    assert not plan.errors
    source.assert_not_called()
    repository.save_system.assert_not_called()
