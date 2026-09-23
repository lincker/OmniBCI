"""Actual composition and lifespan publish one guarded package/market workflow."""
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import omnibci.conf as conf
from omnibci.application.plugin_profiles import PluginProfileStorageError
from omnibci.bootstrap import build_container
from omnibci.presentation.main import create_app
from tests.infrastructure.test_market_repository import _NOW, _URL, _DOWNLOAD, _Opener, _index, _signed
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture(autouse=True)
def isolated_composition(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(tmp_path / "configuration"))
    for name in ("OMNIBCI_MARKET_URL", "OMNIBCI_MARKET_REPOSITORY_ID", "OMNIBCI_MARKET_TRUSTED_KEYS", "OMNIBCI_MARKET_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    settings_path = tmp_path / "settings.toml"
    settings_path.write_bytes(conf.settings._config_path.read_bytes())
    settings = conf.OmniBCISettings(settings_path)
    settings.set("STIMULATION.auto_spawn", False)
    settings.set("ACTUATION.auto_spawn", False)
    settings.set("ACTUATION.enabled", False)
    settings.set("PERSISTENCE.session_repository", "in_memory")
    settings.set("ACQUISITION.signal_source", "file_reader")
    monkeypatch.setattr(conf, "settings", settings)
    builder = Mock(side_effect=AssertionError("Package APIs must not construct an engine graph"))
    monkeypatch.setattr("omnibci.bootstrap.NeuroEngineBuilder.build", builder)
    yield builder
    builder.assert_not_called()


def _configure_market(monkeypatch, tmp_path):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("OMNIBCI_MARKET_URL", _URL)
    monkeypatch.setenv("OMNIBCI_MARKET_REPOSITORY_ID", "example.plugins")
    monkeypatch.setenv("OMNIBCI_MARKET_TRUSTED_KEYS", json.dumps({
        "test-key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
    }))
    monkeypatch.setenv("OMNIBCI_MARKET_CACHE_DIR", str(tmp_path / "cache"))
    return key


@pytest.mark.asyncio
async def test_actual_container_shares_package_store_profiles_and_runtime_gate(tmp_path):
    container = await build_container()
    try:
        packages = container.plugin_package_service
        assert container.plugin_installation_service is packages
        assert packages.profiles is container.plugin_profile_service
        assert packages.store.plugin_dir == tmp_path / "plugins"
        assert packages.admission_guard.__self__ is container.engine_service.admission
        assert packages.profiles.change_guard.__self__ is container.engine_service
        assert container.plugin_market_service is None
        assert container.engine_service.is_initialized and not container.engine_service.is_engine_ready
        assert container.engine_service._engine is None

        installed = packages.install(package_archive(tmp_path)).package
        assert not installed.enabled and packages.list() == [installed]
        with container.engine_service.admission.command():
            with pytest.raises(RuntimeError, match="in progress"):
                packages.set_enabled(installed.package_id, installed.version, True)
    finally:
        await container.engine_service.shutdown()


@pytest.mark.asyncio
async def test_operator_configured_market_uses_same_installer_and_admission(monkeypatch, tmp_path):
    _configure_market(monkeypatch, tmp_path)
    container = await build_container()
    try:
        market = container.plugin_market_service
        packages = container.plugin_package_service
        assert market.repository.store is packages.store
        assert market.repository.repository.installer is packages.store.installer
        assert market.admission_guard.__self__ is container.engine_service.admission
        assert market.repository.repository.expected_repository_id == "example.plugins"
        assert market.repository.repository.cache_dir == tmp_path / "cache"
        assert not (tmp_path / "cache").exists()
    finally:
        await container.engine_service.shutdown()


def test_real_lifespan_routes_install_enable_save_references_and_remove(tmp_path):
    app = create_app()
    with TestClient(app) as client:
        container = app.state.container
        assert app.state.plugin_installation_service is container.plugin_package_service
        assert app.state.plugin_package_service is container.plugin_package_service
        assert app.state.plugin_market_service is None
        assert client.get("/api/v1/market/catalog").status_code == 503
        assert client.post("/api/v1/market/install", json={"package_id": "acme.device", "version": "1.0.0"}).status_code == 503

        response = client.post("/api/v1/plugin-packages/install", content=package_archive(tmp_path).read_bytes(),
                               headers={"Content-Type": "application/zip"})
        assert response.status_code == 201, response.text
        installed = response.json()["data"]["package"]
        assert not installed["enabled"]
        selection = installed["contributions"][0]["selection"]
        assert client.get("/api/v1/plugin-packages").json()["data"] == [installed]
        body = {"kind": "effector", "selection": selection, "name": "Managed output", "parameters": {"gain": 2.0}}
        assert client.post("/api/v1/config/plugin-profiles", json=body).status_code == 422
        path = "/api/v1/plugin-packages/acme.device/1.0.0"
        assert client.put(path + "/enabled", json={"enabled": True}).status_code == 200
        saved = client.post("/api/v1/config/plugin-profiles", json=body)
        assert saved.status_code == 201, saved.text
        profile = saved.json()["data"]
        assert profile["selection"] == selection
        inventory = client.get("/api/v1/plugin-packages").json()["data"][0]
        assert any(ref["scope"] == "profile" and ref["identifier"] == profile["profile_id"] for ref in inventory["references"])
        assert client.put(path + "/enabled", json={"enabled": False}).status_code == 409
        assert client.delete(path).status_code == 409

        before = client.get("/api/v1/config/activation").json()["data"]
        original_bindings = [{"slot": item["slot"], "profile_id": item["profile_id"]} for item in before["bindings"]] if before else []
        bindings = [item for item in original_bindings if item["slot"] != "actuation.effector"]
        accepted = client.put("/api/v1/config/activation", json={
            "bindings": [*bindings, {"slot": "actuation.effector", "profile_id": profile["profile_id"]}],
            "expected_revision": before["revision"] if before else None,
        })
        assert accepted.status_code == 200, accepted.text
        inventory = client.get("/api/v1/plugin-packages").json()["data"][0]
        assert {ref["scope"] for ref in inventory["references"]} == {"profile", "accepted"}
        profile_path = f"/api/v1/config/plugin-profiles/{profile['profile_id']}?expected_revision=1"
        assert client.delete(profile_path).status_code == 409
        restored = client.put("/api/v1/config/activation", json={
            "bindings": original_bindings, "expected_revision": accepted.json()["data"]["revision"],
        })
        assert restored.status_code == 200, restored.text
        assert client.delete(profile_path).status_code == 200
        assert client.put(path + "/enabled", json={"enabled": False}).status_code == 200
        assert client.delete(path).status_code == 200
        assert client.get("/api/v1/plugin-packages").json()["data"] == []
        assert container.engine_service._engine is None


def test_lifespan_http_mutations_share_apply_gate_and_keep_inventory_readable(tmp_path):
    app = create_app()
    with TestClient(app) as client:
        content = package_archive(tmp_path).read_bytes()
        installed = client.post("/api/v1/plugin-packages/install", content=content, headers={"Content-Type": "application/zip"})
        assert installed.status_code == 201
        selection = installed.json()["data"]["package"]["contributions"][0]["selection"]
        entered, release = Event(), Event()
        def occupied():
            with app.state.container.engine_service.admission.transition():
                entered.set()
                assert release.wait(5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            operation = executor.submit(occupied)
            try:
                assert entered.wait(2)
                assert client.get("/api/v1/plugin-packages").status_code == 200
                assert client.post("/api/v1/plugin-packages/install", content=content, headers={"Content-Type": "application/zip"}).status_code == 409
                assert client.put("/api/v1/plugin-packages/acme.device/1.0.0/enabled", json={"enabled": True}).status_code == 409
                assert client.delete("/api/v1/plugin-packages/acme.device/1.0.0").status_code == 409
                assert client.post("/api/v1/config/plugin-profiles", json={
                    "kind": "effector", "selection": selection, "name": "Blocked", "parameters": {},
                }).status_code == 409
            finally:
                release.set()
            operation.result(timeout=2)


def test_configured_market_is_published_by_real_lifespan_and_installs_disabled(monkeypatch, tmp_path):
    key = _configure_market(monkeypatch, tmp_path)
    archive = package_archive(tmp_path).read_bytes()
    index = _index(archive)
    index["packages"][0]["contributions"] = [{"id": "output", "kind": "effector"}]
    app = create_app()
    with TestClient(app) as client:
        market = app.state.plugin_market_service
        assert market is app.state.container.plugin_market_service
        repository = market.repository.repository
        repository.clock = lambda: _NOW
        repository._opener = _Opener({_URL: _signed(index, key), _DOWNLOAD: archive})
        catalog = client.get("/api/v1/market/catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["data"][0]["repository_id"] == "example.plugins"
        installed = client.post("/api/v1/market/install", json={"package_id": "acme.device", "version": "1.0.0"})
        assert installed.status_code == 201, installed.text
        package = installed.json()["data"]["package"]
        assert not package["enabled"] and package["available"]
        assert client.get("/api/v1/plugin-packages").json()["data"] == [package]
        assert repository.installer is app.state.plugin_package_service.store.installer
        assert not list((tmp_path / "plugins").rglob("__pycache__"))


@pytest.mark.parametrize("problem,status", [("invalid", 422), ("unreadable", 503)])
def test_inventory_reports_profile_storage_failures_as_actionable_errors(monkeypatch, problem, status):
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        repository = app.state.container.plugin_profile_service.repository
        if problem == "invalid":
            directory = repository.root / "plugin-profiles"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "00000000-0000-0000-0000-000000000001.toml").write_text("broken = [not valid TOML")
        else:
            monkeypatch.setattr(repository, "list_profiles", Mock(side_effect=PluginProfileStorageError("Profiles unavailable")))
        response = client.get("/api/v1/plugin-packages")
        assert response.status_code == status, response.text
        assert response.json()["success"] is False
