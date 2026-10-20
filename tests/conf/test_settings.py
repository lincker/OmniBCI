"""Host settings and worker snapshots have no named application overlay."""

import pytest

from omnibci.conf import OmniBCISettings


def test_bundled_defaults_contain_only_host_configuration():
    from omnibci.application.activation_settings import BACKEND_STARTUP_DEFAULTS, RUNTIME_HOST_SETTING_KEYS

    defaults = OmniBCISettings().saved_snapshot()

    assert set(defaults) <= RUNTIME_HOST_SETTING_KEYS | BACKEND_STARTUP_DEFAULTS.keys()
    assert defaults["NEUROENGINE.queue_size"] == 100


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        '[ACQUISITION]\nsignal_source = "file_reader"\nsample_rate = 1000\n'
        '[NEUROENGINE]\nqueue_size = 100\n', encoding="utf-8",
    )
    return path


def test_retired_profile_section_is_excluded_from_settings(settings_file):
    with settings_file.open("a", encoding="utf-8") as stream:
        stream.write('[PROFILES]\nactive = "old-profile"\n')
    original_settings = settings_file.read_bytes()

    settings = OmniBCISettings(settings_file)

    assert settings.get("ACQUISITION.sample_rate") == 1000
    assert settings.get("NEUROENGINE.queue_size") == 100
    assert settings.snapshot() == {
        "ACQUISITION.signal_source": "file_reader",
        "ACQUISITION.sample_rate": 1000,
        "NEUROENGINE.queue_size": 100,
    }
    assert settings_file.read_bytes() == original_settings


def test_get_supports_defaults_and_reports_missing_keys(settings_file):
    settings = OmniBCISettings(settings_file)
    assert settings.get("ACQUISITION.signal_source") == "file_reader"
    assert settings.get("MISSING.key", "fallback") == "fallback"
    assert settings.get("MISSING.key", None) is None
    with pytest.raises(NameError, match="MISSING.key"):
        settings.get("MISSING.key")


def test_saved_host_values_survive_reload(settings_file):
    settings = OmniBCISettings(settings_file)
    settings.set("NEUROENGINE.queue_size", 200)
    settings.save()
    assert OmniBCISettings(settings_file).get("NEUROENGINE.queue_size") == 200


def test_worker_snapshot_is_isolated_from_disk_and_caller_changes(settings_file):
    settings = OmniBCISettings(settings_file)
    original = settings_file.read_bytes()
    runtime = {"NEUROENGINE.queue_size": 250, "PLUGINS.bindings": [{"parameters": {"size": 3}}]}
    settings.use_snapshot(runtime)
    runtime["PLUGINS.bindings"][0]["parameters"]["size"] = 7
    returned = settings.snapshot()
    returned["PLUGINS.bindings"][0]["parameters"]["size"] = 9

    assert settings.get("NEUROENGINE.queue_size") == 250
    assert settings.get("PLUGINS.bindings")[0]["parameters"]["size"] == 3
    assert settings.get("ACQUISITION.sample_rate", None) is None
    with pytest.raises(NameError, match="ACQUISITION.sample_rate"):
        settings.get("ACQUISITION.sample_rate")
    assert settings_file.read_bytes() == original


def test_snapshot_and_get_return_independent_containers(settings_file):
    settings = OmniBCISettings(settings_file)
    settings.set("ALGORITHM.filters", ["first"])
    snapshot = settings.snapshot()
    snapshot["ALGORITHM.filters"].append("second")
    assert settings.snapshot()["ALGORITHM.filters"] == ["first"]


def test_missing_settings_file_fails_explicitly(tmp_path):
    with pytest.raises(FileNotFoundError):
        OmniBCISettings(tmp_path / "missing.toml")
