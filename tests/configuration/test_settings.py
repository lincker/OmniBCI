"""Configuration storage exposes owned values without process-global state."""

from omnibci.application.configuration.definitions import SERVER_STARTUP_DEFAULTS
from omnibci.application.configuration.models import EngineSettings
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


def test_server_defaults_are_declared_without_creating_files(tmp_path):
    root = tmp_path / "configuration"
    repository = TomlConfigurationRepository(root)
    values, revision = repository.read_server()
    assert values == SERVER_STARTUP_DEFAULTS
    assert repository.read_server() == (values, revision)
    assert repository.get_engine() is None
    assert not root.exists()


def test_server_reads_are_independent_values(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    values, revision = repository.read_server()
    values["LOGGING.level"] = "DEBUG"
    assert repository.read_server() == (SERVER_STARTUP_DEFAULTS, revision)


def test_module_and_engine_settings_are_not_in_server_values(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 200, "debug": True}))
    assert repository.read_server()[0] == SERVER_STARTUP_DEFAULTS
    assert repository.get_engine().settings == {"NEUROENGINE.queue_size": 200, "debug": True}
