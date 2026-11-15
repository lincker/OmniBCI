"""Engine review uses submitted values independently of server startup edits."""
from unittest.mock import Mock

import pytest

from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS
from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.configuration.models import EngineSettings
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", True), ("NEUROENGINE.queue_size", 0),
    ("NEUROENGINE.queue_size", 1.5), ("NEUROENGINE.queue_size", "100"),
    ("NEUROENGINE.startup_timeout_s", False), ("NEUROENGINE.startup_timeout_s", 0),
    ("NEUROENGINE.startup_timeout_s", -1), ("NEUROENGINE.startup_timeout_s", float("inf")),
    ("NEUROENGINE.startup_timeout_s", 10 ** 400), ("debug", 1), ("debug", "false"),
])
def test_invalid_engine_values_cannot_be_accepted(key, value):
    with pytest.raises(PluginProfileValidationError):
        EngineSettings({key: value})


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
    runtime = RuntimeConfigurationService(profiles, engine)
    with pytest.raises(PluginSchemaError, match="Current Engine settings are required"):
        runtime.plan("engine", [], expected_revision=None)
    plan = runtime.plan("engine", [], expected_revision=None, settings={"NEUROENGINE.queue_size": 42})
    assert plan.strategy == "select_on_start" and not plan.errors
    assert runtime.resolve_plan(plan.plan_id).engine.settings == {"NEUROENGINE.queue_size": 42}
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
    runtime = RuntimeConfigurationService(profiles, engine)

    plan = runtime.plan("processing", [], expected_revision=None,
                        settings={"ALGORITHM.baseline_duration": 1})

    assert runtime.resolve_plan(plan.plan_id).engine.settings == ENGINE_SETTING_DEFAULTS
    assert not plan.errors
    repository.read_server.assert_not_called()
    repository.save_engine.assert_not_called()
