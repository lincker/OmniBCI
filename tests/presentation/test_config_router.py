"""
/api/v1/config endpoints — layered configuration (stage 4).

api_client's real ConfigService (wired to the real settings singleton) is
replaced per test with one over a temp settings.toml.
"""
import pytest

from omnibci.application.services.config_service import ConfigService
from omnibci.conf import OmniBCISettings


@pytest.fixture()
def config_client(api_client, tmp_path):
    toml = tmp_path / "settings.toml"
    toml.write_text(
        "[ACQUISITION]\n"
        "signal_source = \"file_reader\"\n"
        "sample_rate = 1000\n"
        "[ALGORITHM]\n"
        "window_duration = 3.0\n"
        "[STIMULATION]\n"
        "paradigm = \"classic40\"\n",
        encoding="utf-8",
    )
    api_client.app.state.config_service = ConfigService(OmniBCISettings(toml_file=toml))
    return api_client


class TestGetConfig:
    def test_shape_and_whitelist(self, config_client):
        resp = config_client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["active_profile"] is None
        keys = {e["key"] for e in data["entries"]}
        assert "ACQUISITION.sample_rate" in keys
        assert "LOGGING.level" not in keys  # whitelist only
        entry = next(e for e in data["entries"] if e["key"] == "ACQUISITION.sample_rate")
        assert entry["default_value"] == 1000
        assert entry["effective_value"] == 1000
        assert entry["requires"] == "engine-restart"


class TestPatchConfig:
    def test_patch_default(self, config_client):
        resp = config_client.patch("/api/v1/config", json={
            "updates": {"ACQUISITION.sample_rate": 500}})
        assert resp.status_code == 200
        body = resp.json()["data"]
        assert body["applied"] == ["ACQUISITION.sample_rate"]
        assert body["engine_restart_required"] is True
        get = config_client.get("/api/v1/config").json()["data"]
        e = next(x for x in get["entries"] if x["key"] == "ACQUISITION.sample_rate")
        assert e["default_value"] == 500

    def test_patch_hot_key_no_restart(self, config_client):
        resp = config_client.patch("/api/v1/config", json={
            "updates": {"STIMULATION.paradigm": "p2"}})
        assert resp.json()["data"]["engine_restart_required"] is False

    def test_unknown_key_400_config_key_not_allowed(self, config_client):
        resp = config_client.patch("/api/v1/config", json={
            "updates": {"LOGGING.level": "DEBUG"}})
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "CONFIG_KEY_NOT_ALLOWED"
        assert body["detail"]["keys"] == ["LOGGING.level"]

    def test_bad_type_422_with_key_detail(self, config_client):
        resp = config_client.patch("/api/v1/config", json={
            "updates": {"ACQUISITION.sample_rate": "fast"}})
        assert resp.status_code == 422
        assert resp.json()["code"] == "VALIDATION_ERROR"
        assert "ACQUISITION.sample_rate" in resp.json()["detail"]

    def test_patch_profile_scope_creates_profile(self, config_client):
        resp = config_client.patch("/api/v1/config", json={
            "updates": {"ACQUISITION.sample_rate": 250}, "scope": "实采",
            "description": "设备实采"})
        assert resp.status_code == 200
        listed = config_client.get("/api/v1/config/profiles").json()["data"]
        assert [p["name"] for p in listed["profiles"]] == ["实采"]
        assert listed["profiles"][0]["description"] == "设备实采"


class TestProfileEndpoints:
    def _mk(self, client, name="p1", rate=500):
        client.put(f"/api/v1/config/profiles/{name}", json={
            "updates": {"ACQUISITION.sample_rate": rate}})

    def test_put_then_detail(self, config_client):
        self._mk(config_client)
        resp = config_client.get("/api/v1/config/profiles/p1")
        assert resp.status_code == 200
        assert resp.json()["data"]["entries"] == {"ACQUISITION.sample_rate": 500}

    def test_detail_404(self, config_client):
        resp = config_client.get("/api/v1/config/profiles/ghost")
        assert resp.status_code == 404
        assert resp.json()["code"] == "CONFIG_PROFILE_NOT_FOUND"

    def test_activate_changes_effective(self, config_client):
        self._mk(config_client)
        resp = config_client.post("/api/v1/config/profiles/p1/activate")
        assert resp.status_code == 200
        data = config_client.get("/api/v1/config").json()["data"]
        assert data["active_profile"] == "p1"
        e = next(x for x in data["entries"] if x["key"] == "ACQUISITION.sample_rate")
        assert (e["default_value"], e["profile_value"], e["effective_value"]) == (1000, 500, 500)

    def test_activate_missing_404(self, config_client):
        resp = config_client.post("/api/v1/config/profiles/ghost/activate")
        assert resp.status_code == 404
        assert resp.json()["code"] == "CONFIG_PROFILE_NOT_FOUND"

    def test_delete_active_409_in_use(self, config_client):
        self._mk(config_client)
        config_client.post("/api/v1/config/profiles/p1/activate")
        resp = config_client.delete("/api/v1/config/profiles/p1")
        assert resp.status_code == 409
        assert resp.json()["code"] == "CONFIG_PROFILE_IN_USE"

    def test_deactivate_then_delete(self, config_client):
        self._mk(config_client)
        config_client.post("/api/v1/config/profiles/p1/activate")
        assert config_client.post("/api/v1/config/profiles/deactivate").status_code == 200
        assert config_client.delete("/api/v1/config/profiles/p1").status_code == 200
        assert config_client.get("/api/v1/config/profiles").json()["data"]["profiles"] == []

    def test_invalid_name_422(self, config_client):
        # "a.b" reaches the handler (valid path segment) but fails name policy;
        # path-separator attempts (%2F) never even match the route → 404.
        resp = config_client.put("/api/v1/config/profiles/a.b", json={
            "updates": {"ACQUISITION.sample_rate": 1}})
        assert resp.status_code == 422
        assert resp.json()["code"] == "VALIDATION_ERROR"
