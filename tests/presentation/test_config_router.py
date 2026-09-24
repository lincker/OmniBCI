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
    assert set(data) == {"entries", "revision"}
    assert len(data["revision"]) == 64
    assert {entry["key"]: entry["value"] for entry in data["entries"]} == {
        "ALGORITHM.baseline_duration": 1.0,
        "NEUROENGINE.queue_size": 32,
    }
    assert all(set(entry) == {"key", "value", "type", "requires", "description"}
               for entry in data["entries"])


def test_patch_saves_host_settings(config_client, tmp_path):
    revision = config_client.get("/api/v1/config").json()["data"]["revision"]
    response = config_client.patch("/api/v1/config", json={"expected_revision": revision, "updates": {
        "NEUROENGINE.queue_size": 64,
        "ALGORITHM.baseline_duration": 2.5,
    }})
    assert response.status_code == 200
    assert response.json()["data"] == {
        "revision": config_client.get("/api/v1/config").json()["data"]["revision"],
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
    revision = config_client.get("/api/v1/config").json()["data"]["revision"]
    response = config_client.patch("/api/v1/config", json={"expected_revision": revision, "updates": {
        "NEUROENGINE.queue_size": 64, key: value,
    }})
    assert response.status_code == 400
    assert response.json()["code"] == "CONFIG_KEY_NOT_ALLOWED"
    assert response.json()["detail"]["keys"] == [key]
    assert path.read_bytes() == before
    assert config_client.app.state.config_service._settings.get("NEUROENGINE.queue_size") == 32


def test_invalid_host_value_has_key_detail(config_client):
    response = config_client.patch("/api/v1/config", json={
        "expected_revision": config_client.get("/api/v1/config").json()["data"]["revision"],
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
        "expected_revision": config_client.get("/api/v1/config").json()["data"]["revision"],
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


@pytest.mark.parametrize("revision", [None, "", "old", "a" * 63, "g" * 64, 123])
def test_patch_requires_valid_revision(config_client, tmp_path, revision):
    before = (tmp_path / "settings.toml").read_bytes()
    body = {"updates": {"NEUROENGINE.queue_size": 64}}
    if revision is not None:
        body["expected_revision"] = revision
    response = config_client.patch("/api/v1/config", json=body)
    assert response.status_code == 422
    assert (tmp_path / "settings.toml").read_bytes() == before


def test_stale_browser_save_returns_conflict_and_preserves_newer_values(config_client):
    revision = config_client.get("/api/v1/config").json()["data"]["revision"]
    first = config_client.patch("/api/v1/config", json={
        "expected_revision": revision, "updates": {"NEUROENGINE.queue_size": 64}})
    assert first.status_code == 200
    updated_revision = first.json()["data"]["revision"]
    assert updated_revision != revision
    stale = config_client.patch("/api/v1/config", json={
        "expected_revision": revision, "updates": {"ALGORITHM.baseline_duration": 3.0}})
    assert stale.status_code == 409
    assert stale.json()["code"] == "CONFLICT"
    data = config_client.get("/api/v1/config").json()["data"]
    assert data["revision"] == updated_revision
    assert {entry["key"]: entry["value"] for entry in data["entries"]} == {
        "ALGORITHM.baseline_duration": 1.0, "NEUROENGINE.queue_size": 64}


@pytest.mark.parametrize("method", ["GET", "PATCH"])
def test_unavailable_storage_returns_service_error(config_client, monkeypatch, method):
    settings = config_client.app.state.config_service._settings
    revision = config_client.get("/api/v1/config").json()["data"]["revision"]

    def fail(*args, **kwargs):
        raise OSError("disk is unavailable")

    monkeypatch.setattr(settings, "read_saved" if method == "GET" else "update", fail)
    response = config_client.request(method, "/api/v1/config", json={
        "expected_revision": revision, "updates": {"NEUROENGINE.queue_size": 64}})
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", 0), ("NEUROENGINE.queue_size", -10),
    ("ALGORITHM.baseline_duration", -1),
])
def test_invalid_numeric_ranges_are_rejected(config_client, key, value):
    revision = config_client.get("/api/v1/config").json()["data"]["revision"]
    response = config_client.patch("/api/v1/config", json={
        "expected_revision": revision, "updates": {key: value}})
    assert response.status_code == 422
    assert key in response.json()["detail"]
    assert config_client.get("/api/v1/config").json()["data"]["revision"] == revision
