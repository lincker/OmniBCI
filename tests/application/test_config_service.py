"""ConfigService exposes saved host settings, separate from plugin profiles."""

import pytest

from omnibci.application.services.config_service import (
    ConfigKeyNotAllowedError,
    ConfigService,
    ConfigValidationError,
)
from omnibci.conf import OmniBCISettings


@pytest.fixture()
def svc(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        '[ACQUISITION]\nsignal_source = "file_reader"\nsample_rate = 1000\n'
        '[ALGORITHM]\nbaseline_duration = 1.0\nwindow_duration = 3.0\n'
        '[NEUROENGINE]\nqueue_size = 32\n', encoding="utf-8",
    )
    return ConfigService(OmniBCISettings(toml_file=path))


def test_lists_only_saved_host_settings(svc):
    entries = svc.list_entries()
    assert {entry["key"]: entry["value"] for entry in entries} == {
        "ALGORITHM.baseline_duration": 1.0,
        "NEUROENGINE.queue_size": 32,
    }
    assert all(set(entry) == {"key", "value", "type", "requires", "description"}
               for entry in entries)
    assert all(entry["requires"] == "engine-restart" for entry in entries)


@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.signal_source", "neuroscan_scan45_reader"),
    ("ACQUISITION.sample_rate", 500),
    ("ACQUISITION.channel_number", 32),
    ("ACQUISITION.data_file_path", "recording.cnt"),
    ("ACQUISITION.parallel_port_address", "0x378"),
    ("ALGORITHM.decoder", "other"),
    ("ALGORITHM.window_duration", 4.0),
    ("ALGORITHM.offset_time", 0.5),
    ("ALGORITHM.filters", ["bandpass"]),
    ("STIMULATION.paradigm", "other"),
    ("ACTUATION.effector", "logger"),
    ("LOGGING.level", "DEBUG"),
])
def test_plugin_and_internal_keys_rejected_without_partial_write(svc, tmp_path, key, value):
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    with pytest.raises(ConfigKeyNotAllowedError) as raised:
        svc.save({"NEUROENGINE.queue_size": 64, key: value})
    assert raised.value.keys == [key]
    assert svc._settings.get("NEUROENGINE.queue_size") == 32
    assert path.read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", "large"),
    ("NEUROENGINE.queue_size", True),
    ("NEUROENGINE.queue_size", 3.5),
    ("ALGORITHM.baseline_duration", "long"),
    ("ALGORITHM.baseline_duration", False),
])
def test_wrong_types_are_rejected_before_any_write(svc, tmp_path, key, value):
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    with pytest.raises(ConfigValidationError) as raised:
        svc.save({"NEUROENGINE.queue_size": 64, key: value})
    assert key in raised.value.bad_keys
    assert svc._settings.get("NEUROENGINE.queue_size") == 32
    assert path.read_bytes() == before


def test_empty_updates_are_rejected(svc):
    with pytest.raises(ConfigValidationError):
        svc.save({})


def test_saves_both_host_settings_and_normalizes_float(svc, tmp_path):
    saved, restart = svc.save({
        "NEUROENGINE.queue_size": 64,
        "ALGORITHM.baseline_duration": 2,
    })
    assert saved == ["ALGORITHM.baseline_duration", "NEUROENGINE.queue_size"]
    assert restart is True
    fresh = OmniBCISettings(tmp_path / "settings.toml")
    assert fresh.get("NEUROENGINE.queue_size") == 64
    assert fresh.get("ALGORITHM.baseline_duration") == 2.0
    assert isinstance(fresh.get("ALGORITHM.baseline_duration"), float)
    assert fresh.get("ACQUISITION.sample_rate") == 1000


def test_save_does_not_change_installed_runtime_snapshot(svc, tmp_path):
    pinned = svc._settings.snapshot()
    svc._settings.use_snapshot(pinned)
    svc.save({"NEUROENGINE.queue_size": 64})
    assert svc._settings.snapshot() == pinned
    assert OmniBCISettings(tmp_path / "settings.toml").get("NEUROENGINE.queue_size") == 64
