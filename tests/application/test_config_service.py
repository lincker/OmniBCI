"""Server settings save independently of Engine application and startup state."""
import math

import pytest

from omnibci.application.configuration.definitions import SERVER_STARTUP_DEFAULTS
from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError, ConfigProfileConflictError, ConfigValidationError,
)
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.configuration.models import EngineSettings, ModuleActivation
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


@pytest.fixture
def svc(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[LOGGING]\nlevel="INFO"\nfile_path="omnibci.log"\n'
                    '[NEUROENGINE]\ncommand_timeout_s=10.0\n[PLUGINS]\nuser_dir=""\n')
    return ConfigurationService(TomlConfigurationRepository(tmp_path))


def test_lists_server_settings_and_values_loaded_at_startup(svc):
    entries, _ = svc.read_server()
    assert {entry["key"]: entry["value"] for entry in entries} == SERVER_STARTUP_DEFAULTS
    assert all(entry["active_value"] == entry["value"] and not entry["restart_required"] for entry in entries)
    assert all(entry["requires"] == "server-restart" for entry in entries)


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", 64), ("NEUROENGINE.startup_timeout_s", 3), ("debug", True),
    ("ACQUISITION.sample_rate", 500), ("ALGORITHM.baseline_duration", 1.0),
    ("STIMULATION.auto_spawn", False), ("ACTUATION.server_port", 8002),
])
def test_engine_and_module_keys_cannot_partially_write_server_settings(svc, tmp_path, key, value):
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    with pytest.raises(ConfigKeyNotAllowedError) as raised:
        svc.save_server({"LOGGING.level": "DEBUG", key: value}, expected_revision=svc.read_server()[1])
    assert raised.value.keys == [key]
    assert path.read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.command_timeout_s", "large"), ("NEUROENGINE.command_timeout_s", True),
    ("NEUROENGINE.command_timeout_s", 0), ("NEUROENGINE.command_timeout_s", -1),
    ("NEUROENGINE.command_timeout_s", math.inf), ("NEUROENGINE.command_timeout_s", math.nan),
    ("LOGGING.level", "verbose"), ("LOGGING.level", False),
    ("LOGGING.file_path", " "), ("LOGGING.file_path", 123), ("PLUGINS.user_dir", False),
])
def test_invalid_server_values_leave_disk_unchanged(svc, tmp_path, key, value):
    before = (tmp_path / "settings.toml").read_bytes()
    with pytest.raises(ConfigValidationError) as raised:
        svc.save_server({key: value}, expected_revision=svc.read_server()[1])
    assert key in raised.value.bad_keys
    assert (tmp_path / "settings.toml").read_bytes() == before


def test_empty_updates_are_rejected(svc):
    with pytest.raises(ConfigValidationError):
        svc.save_server({}, expected_revision=svc.read_server()[1])


def test_saved_values_remain_pending_until_server_restart(svc, tmp_path):
    saved, restart, revision = svc.save_server({"LOGGING.level": "DEBUG", "NEUROENGINE.command_timeout_s": 20},
                                      expected_revision=svc.read_server()[1])
    assert saved == ["LOGGING.level", "NEUROENGINE.command_timeout_s"] and restart
    entries, current_revision = svc.read_server()
    assert current_revision == revision
    values = {entry["key"]: entry for entry in entries}
    assert values["LOGGING.level"]["value"] == "DEBUG"
    assert values["LOGGING.level"]["active_value"] == "INFO"
    assert values["LOGGING.level"]["restart_required"]
    restarted = ConfigurationService(TomlConfigurationRepository(tmp_path))
    assert all(not entry["restart_required"] for entry in restarted.read_server()[0])


def test_explicit_startup_snapshot_is_not_replaced_by_file_edits(svc):
    active = {**SERVER_STARTUP_DEFAULTS, "LOGGING.level": "WARNING"}
    service = ConfigurationService(svc.repository, active_server_settings=active)
    active["LOGGING.level"] = "ERROR"
    item = next(entry for entry in service.read_server()[0] if entry["key"] == "LOGGING.level")
    assert item["active_value"] == "WARNING" and item["restart_required"]
    _, restart, _ = service.save_server({"LOGGING.level": "WARNING"}, expected_revision=service.read_server()[1])
    assert not restart


def test_server_save_preserves_engine_and_module_configuration(svc):
    engine = EngineSettings({"NEUROENGINE.queue_size": 32})
    module = ModuleActivation("stimulation", {"STIMULATION.server_port": 9001}, ())
    svc.repository.save_engine(engine, expected_revision=None)
    svc.repository.save_module(module, expected_revision=None)

    svc.save_server({"LOGGING.level": "DEBUG"}, expected_revision=svc.read_server()[1])

    assert svc.repository.get_engine().to_dict() == engine.to_dict()
    assert svc.repository.get_module("stimulation").to_dict() == module.to_dict()


def test_stale_browser_cannot_overwrite_another_save(svc, tmp_path):
    other = ConfigurationService(TomlConfigurationRepository(tmp_path))
    revision = svc.read_server()[1]
    _, _, new_revision = other.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    with pytest.raises(ConfigProfileConflictError):
        svc.save_server({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert svc.read_server()[1] == new_revision
    assert next(entry["value"] for entry in svc.read_server()[0] if entry["key"] == "LOGGING.level") == "DEBUG"


def test_storage_failure_keeps_saved_and_active_state(svc, tmp_path, monkeypatch):
    before = (tmp_path / "settings.toml").read_bytes()
    original = svc.read_server()
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(svc.repository, "save_server", fail)
    with pytest.raises(OSError, match="disk full"):
        svc.save_server({"LOGGING.level": "DEBUG"}, expected_revision=original[1])
    assert svc.read_server() == original
    assert (tmp_path / "settings.toml").read_bytes() == before
