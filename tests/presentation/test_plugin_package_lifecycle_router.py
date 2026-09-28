"""Package lifecycle routes expose reference conflicts and explicit profile copies."""
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.services.plugin_package_service import PluginPackageService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_package_lifecycle import router
from omnibci.presentation.routers.plugin_packages import router as installation_router
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: {})
    svc = PluginPackageService(ManagedPluginPackageStore(), profiles, runtime_references=lambda: (), admission_guard=nullcontext)
    for version in ("1.0.0", "2.0.0"):
        svc.install(package_archive(tmp_path, version))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(installation_router)
    app.include_router(router)
    app.state.plugin_installation_service = svc
    app.state.plugin_package_service = svc
    with TestClient(app) as http:
        yield http, svc


def test_enable_referenced_disable_and_remove_http_contract(client):
    http, svc = client
    path = "/plugin-packages/acme.device/1.0.0"
    enabled = http.put(f"{path}/enabled", json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json()["data"]["enabled"]
    selection = svc.store.get_package("acme.device", "1.0.0").contributions[0].selection
    profile = svc.profiles.create("effector", selection, "Example", {})
    assert http.get("/plugin-packages").json()["data"][0]["references"][0]["identifier"] == profile.profile_id
    assert http.put(f"{path}/enabled", json={"enabled": False}).status_code == 409
    assert http.delete(path).status_code == 409
    svc.profiles.delete(profile.profile_id, expected_revision=1)
    assert http.put(f"{path}/enabled", json={"enabled": False}).status_code == 200
    assert http.delete(path).status_code == 200
    assert http.delete(path).status_code == 404


def test_upgrade_preview_then_save_new_profile_with_revision_guard(client):
    http, svc = client
    svc.set_enabled("acme.device", "1.0.0", True)
    selection = svc.store.get_package("acme.device", "1.0.0").contributions[0].selection
    original = svc.profiles.create("effector", selection, "Output", {"gain": 2.5})
    body = {"source_profile_id": original.profile_id, "target_version": "2.0.0", "name": "Output v2"}
    preview = http.post("/plugin-packages/profile-version-proposals", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["parameters"] == {"gain": 2.5}
    assert not preview.json()["data"]["target_enabled"]
    body["expected_source_revision"] = 1
    assert http.post("/plugin-packages/version-profiles", json=body).status_code == 409
    svc.set_enabled("acme.device", "2.0.0", True)
    saved = http.post("/plugin-packages/version-profiles", json=body)
    assert saved.status_code == 201, saved.text
    assert saved.json()["data"]["profile_id"] != original.profile_id
    assert saved.json()["data"]["selection"]["version"] == "2.0.0"
    body["expected_source_revision"] = 99
    assert http.post("/plugin-packages/version-profiles", json=body).status_code == 409
    assert svc.profiles.repository.get_activation() is None


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": True, "unknown": 1}])
def test_strict_enabled_contract(client, body):
    http, _ = client
    assert http.put("/plugin-packages/acme.device/1.0.0/enabled", json=body).status_code == 422


def test_missing_lifecycle_service_is_unavailable(client):
    http, _ = client
    del http.app.state.plugin_package_service
    assert http.delete("/plugin-packages/acme.device/1.0.0").status_code == 503
