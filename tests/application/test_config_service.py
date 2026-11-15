"""Server settings save independently of Engine application and startup state."""
import math

import pytest

from omnibci.application.activation_settings import SERVER_STARTUP_DEFAULTS
from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.services.config_service import (
    ConfigKeyNotAllowedError, ConfigService, ConfigValidationError,
)
from omnibci.conf import OmniBCISettings


@pytest.fixture
def svc(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[LOGGING]\nlevel="INFO"\nfile_path="omnibci.log"\n'
                    '[NEUROENGINE]\ncommand_timeout_s=10.0\n[PLUGINS]\nuser_dir=""\n')
    return ConfigService(OmniBCISettings(toml_file=path))


def test_lists_server_settings_and_values_loaded_at_startup(svc):
    entries, _ = svc.read()
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
        svc.save({"LOGGING.level": "DEBUG", key: value}, expected_revision=svc.read()[1])
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
        svc.save({key: value}, expected_revision=svc.read()[1])
    assert key in raised.value.bad_keys
    assert (tmp_path / "settings.toml").read_bytes() == before


def test_empty_updates_are_rejected(svc):
    with pytest.raises(ConfigValidationError):
        svc.save({}, expected_revision=svc.read()[1])


def test_saved_values_remain_pending_until_server_restart(svc, tmp_path):
    saved, restart, revision = svc.save({"LOGGING.level": "DEBUG", "NEUROENGINE.command_timeout_s": 20},
                                      expected_revision=svc.read()[1])
    assert saved == ["LOGGING.level", "NEUROENGINE.command_timeout_s"] and restart
    entries, current_revision = svc.read()
    assert current_revision == revision
    values = {entry["key"]: entry for entry in entries}
    assert values["LOGGING.level"]["value"] == "DEBUG"
    assert values["LOGGING.level"]["active_value"] == "INFO"
    assert values["LOGGING.level"]["restart_required"]
    restarted = ConfigService(OmniBCISettings(tmp_path / "settings.toml"))
    assert all(not entry["restart_required"] for entry in restarted.list_entries())


def test_explicit_startup_snapshot_is_not_replaced_by_file_edits(svc):
    active = {**SERVER_STARTUP_DEFAULTS, "LOGGING.level": "WARNING"}
    service = ConfigService(svc._settings, active_settings=active)
    active["LOGGING.level"] = "ERROR"
    item = next(entry for entry in service.list_entries() if entry["key"] == "LOGGING.level")
    assert item["active_value"] == "WARNING" and item["restart_required"]
    _, restart, _ = service.save({"LOGGING.level": "WARNING"}, expected_revision=service.read()[1])
    assert not restart


def test_save_does_not_change_installed_worker_snapshot(svc, tmp_path):
    pinned = svc._settings.snapshot()
    svc._settings.use_snapshot(pinned)
    svc.save({"LOGGING.level": "DEBUG"}, expected_revision=svc.read()[1])
    assert svc._settings.snapshot() == pinned
    assert OmniBCISettings(tmp_path / "settings.toml").get("LOGGING.level") == "DEBUG"


def test_stale_browser_cannot_overwrite_another_save(svc, tmp_path):
    other = ConfigService(OmniBCISettings(tmp_path / "settings.toml"))
    revision = svc.read()[1]
    _, _, new_revision = other.save({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    with pytest.raises(ConfigProfileConflictError):
        svc.save({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert svc.read()[1] == new_revision
    assert next(entry["value"] for entry in svc.list_entries() if entry["key"] == "LOGGING.level") == "DEBUG"


def test_storage_failure_keeps_saved_and_active_state(svc, tmp_path, monkeypatch):
    before = (tmp_path / "settings.toml").read_bytes()
    original = svc.read()
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(svc._settings, "update", fail)
    with pytest.raises(OSError, match="disk full"):
        svc.save({"LOGGING.level": "DEBUG"}, expected_revision=original[1])
    assert svc.read() == original
    assert (tmp_path / "settings.toml").read_bytes() == before
