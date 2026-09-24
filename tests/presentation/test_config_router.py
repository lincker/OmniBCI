"""Host settings API rejects plugin fields and retired application profiles."""

import pytest

from omnibci.application.services.config_service import ConfigService
from omnibci.conf import OmniBCISettings


@pytest.fixture()
def config_client(api_client, tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        '[ACQUISITION]\nsignal_source = "file_reader"\nsample_rate = 1000\n'
        '[ALGORITHM]\nbaseline_duration = 1.0\n'
        '[NEUROENGINE]\nqueue_size = 32\n', encoding="utf-8",
    )
    api_client.app.state.config_service = ConfigService(OmniBCISettings(path))
    return api_client


def test_get_saved_settings_shape(config_client):
    response = config_client.get("/api/v1/config")
    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"entries"}
    assert {entry["key"]: entry["value"] for entry in data["entries"]} == {
        "ALGORITHM.baseline_duration": 1.0,
        "NEUROENGINE.queue_size": 32,
    }
    assert all(set(entry) == {"key", "value", "type", "requires", "description"}
               for entry in data["entries"])


def test_patch_saves_host_settings(config_client, tmp_path):
    response = config_client.patch("/api/v1/config", json={"updates": {
        "NEUROENGINE.queue_size": 64,
        "ALGORITHM.baseline_duration": 2.5,
    }})
    assert response.status_code == 200
    assert response.json()["data"] == {
        "saved": ["ALGORITHM.baseline_duration", "NEUROENGINE.queue_size"],
        "engine_restart_required": True,
    }
    fresh = OmniBCISettings(tmp_path / "settings.toml")
    assert fresh.get("NEUROENGINE.queue_size") == 64
    assert fresh.get("ALGORITHM.baseline_duration") == 2.5


@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", 500),
    ("ACQUISITION.parallel_port_address", "0x378"),
    ("ACQUISITION.file_reader_play_speed", 2.0),
    ("ALGORITHM.decoder", "other"),
    ("ALGORITHM.filters", []),
    ("STIMULATION.paradigm", "other"),
    ("ACTUATION.effector", "logger"),
    ("LOGGING.level", "DEBUG"),
])
def test_plugin_and_internal_keys_cannot_partially_write(config_client, tmp_path, key, value):
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    response = config_client.patch("/api/v1/config", json={"updates": {
        "NEUROENGINE.queue_size": 64, key: value,
    }})
    assert response.status_code == 400
    assert response.json()["code"] == "CONFIG_KEY_NOT_ALLOWED"
    assert response.json()["detail"]["keys"] == [key]
    assert path.read_bytes() == before
    assert config_client.app.state.config_service._settings.get("NEUROENGINE.queue_size") == 32


def test_invalid_host_value_has_key_detail(config_client):
    response = config_client.patch("/api/v1/config", json={
        "updates": {"NEUROENGINE.queue_size": "large"},
    })
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "NEUROENGINE.queue_size" in response.json()["detail"]


@pytest.mark.parametrize("extra", [
    {"scope": "default"}, {"scope": "Lab"}, {"description": "Old profile"},
])
def test_retired_patch_fields_are_rejected(config_client, tmp_path, extra):
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    response = config_client.patch("/api/v1/config", json={
        "updates": {"NEUROENGINE.queue_size": 64}, **extra,
    })
    assert response.status_code == 422
    assert path.read_bytes() == before


@pytest.mark.parametrize("method,path", [
    ("GET", "/profiles"), ("GET", "/profiles/Lab"),
    ("PUT", "/profiles/Lab"), ("DELETE", "/profiles/Lab"),
    ("POST", "/profiles/Lab/activate"), ("POST", "/profiles/deactivate"),
])
def test_application_profile_routes_are_removed(config_client, method, path):
    response = config_client.request(method, "/api/v1/config" + path, json={
        "updates": {"NEUROENGINE.queue_size": 64},
    })
    assert response.status_code == 404
