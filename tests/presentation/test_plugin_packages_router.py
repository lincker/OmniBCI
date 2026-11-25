"""Real bounded ZIP upload through the installation-only HTTP adapter."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.configuration.errors import PluginProfileStorageError, PluginProfileValidationError, ProfileRevisionConflict
from omnibci.application.plugin_management.package_service import PluginPackageService
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_packages import router
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    service = PluginPackageService(ManagedPluginPackageStore())
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.plugin_package_service = service
    with TestClient(app) as http:
        yield http, service


def test_raw_upload_installs_once_without_enabling(client, tmp_path):
    http, _ = client
    content = package_archive(tmp_path).read_bytes()
    response = http.post("/plugin-packages/install", content=content, headers={"Content-Type": "application/zip"})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert not data["already_installed"] and not data["package"]["enabled"]
    assert data["package"]["contributions"][0]["selection"]["version"] == "1.0.0"
    assert "location" not in data["package"]
    repeated = http.post("/plugin-packages/install", content=content, headers={"Content-Type": "application/octet-stream"})
    assert repeated.json()["data"]["already_installed"]
    assert http.get("/plugin-packages").json()["data"] == [data["package"]]


def test_chunked_upload_without_content_length_is_counted(client, tmp_path):
    http, service = client
    service.store.max_archive_bytes = 10
    response = http.post("/plugin-packages/install", content=iter([b"a" * 6, b"b" * 6]),
                         headers={"Content-Type": "application/zip"})
    assert response.status_code == 413
    assert service.list() == []


@pytest.mark.parametrize("headers,content,status", [
    ({"Content-Type": "text/plain"}, b"zip", 415),
    ({"Content-Type": "application/zip", "Content-Length": "999999999999999999999999"}, b"zip", 413),
    ({"Content-Type": "application/zip", "Content-Length": "12"}, b"zip", 422),
    ({"Content-Type": "application/zip"}, b"", 422),
    ({"Content-Type": "application/zip"}, b"invalid", 422),
])
def test_invalid_uploads_are_actionable_and_leave_no_installation(client, headers, content, status):
    http, service = client
    response = http.post("/plugin-packages/install", content=content, headers=headers)
    assert response.status_code == status, response.text
    assert service.list() == []


def test_in_progress_configuration_change_rejects_installation(client, tmp_path):
    http, service = client
    @contextmanager
    def occupied():
        raise ProfileRevisionConflict("Apply in progress")
        yield
    service.admission_guard = occupied
    response = http.post("/plugin-packages/install", content=package_archive(tmp_path).read_bytes(),
                         headers={"Content-Type": "application/zip"})
    assert response.status_code == 409
    assert not service.list()


def test_unconfigured_installation_returns_unavailable(client):
    http, _ = client
    del http.app.state.plugin_package_service
    assert http.get("/plugin-packages").status_code == 503


@pytest.mark.parametrize("error,status", [
    (PluginProfileValidationError("Invalid profile document"), 422),
    (PluginProfileStorageError("Cannot read profile storage"), 503),
])
def test_inventory_reference_failures_use_configuration_error_contract(client, monkeypatch, error, status):
    http, service = client
    monkeypatch.setattr(service, "list", Mock(side_effect=error))
    response = http.get("/plugin-packages")
    assert response.status_code == status
    assert response.json()["success"] is False
    assert response.json()["message"] == str(error)


from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
from omnibci.application.configuration.service import ConfigurationService
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.plugin_configuration_helpers import builtin_selection


@pytest.fixture
def lifecycle_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    store = ManagedPluginPackageStore()
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), profile_versions=store)
    svc = PluginPackageService(store, configuration_references=profiles.package_references, runtime_references=lambda: (), admission_guard=nullcontext)
    for version in ("1.0.0", "2.0.0"):
        svc.install(package_archive(tmp_path, version))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.container = SimpleNamespace(configuration_service=profiles)
    app.state.plugin_package_service = svc
    with TestClient(app) as http:
        yield http, svc, profiles


def test_enable_referenced_disable_and_remove_http_contract(lifecycle_client):
    http, svc, profiles = lifecycle_client
    path = "/plugin-packages/acme.device/1.0.0"
    enabled = http.put(f"{path}/enabled", json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json()["data"]["enabled"]
    selection = svc.store.get_package("acme.device", "1.0.0").contributions[0].selection
    profile = profiles.create("effector", selection, "Example", {})
    assert http.get("/plugin-packages").json()["data"][0]["references"][0]["identifier"] == profile.profile_id
    assert http.put(f"{path}/enabled", json={"enabled": False}).status_code == 409
    assert http.delete(path).status_code == 409
    profiles.delete(profile.profile_id, expected_revision=1)
    assert http.put(f"{path}/enabled", json={"enabled": False}).status_code == 200
    assert http.delete(path).status_code == 200
    assert http.delete(path).status_code == 404


def test_upgrade_preview_then_save_new_profile_with_revision_guard(lifecycle_client):
    http, svc, profiles = lifecycle_client
    svc.set_enabled("acme.device", "1.0.0", True)
    selection = svc.store.get_package("acme.device", "1.0.0").contributions[0].selection
    original = profiles.create("effector", selection, "Output", {"gain": 2.5})
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
    assert all(module is None for module in profiles.selected_configuration().modules.values())


@pytest.mark.parametrize("kind", ["decoder", "acquisition"])
def test_version_proposal_serializes_channel_configuration(lifecycle_client, monkeypatch, kind):
    http, svc, profiles = lifecycle_client
    alias = "THUBCI_SSVEP_classic40CCA" if kind == "decoder" else "file_reader"
    definition = profiles.catalog.get(kind, builtin_selection(kind, alias))
    values = {"processing_input": ProcessingInput(outputs=(ChannelBinding("O2", "channel-1", "eeg"), ChannelBinding("O1", "channel-0", "eeg")))} if kind == "decoder" else {}
    parameters = {} if kind == "decoder" else {"sample_rate": 500, "channel_count": 2}
    source = profiles.create(kind, definition.selection, "Versioned profile", parameters,
                                 **(values if kind == "decoder" else {}))
    target = replace(definition, selection=replace(definition.selection, version="99.0.0"))
    monkeypatch.setattr(svc.store, "get_package", lambda *_: SimpleNamespace(error=None, available=True, enabled=True))
    monkeypatch.setattr(svc.store, "configuration", lambda *_: target)
    monkeypatch.setattr(profiles.catalog, "get", lambda *_: target)
    body = {"source_profile_id": source.profile_id, "target_version": "99.0.0"}
    proposal = http.post("/plugin-packages/profile-version-proposals", json=body)
    assert proposal.status_code == 200, proposal.text
    copied = http.post("/plugin-packages/version-profiles", json={**body, "expected_source_revision": source.revision})
    assert copied.status_code == 201, copied.text
    if kind == "decoder":
        assert copied.json()["data"]["processing_input"] == proposal.json()["data"]["processing_input"]
    else:
        assert "manual_montage" not in copied.json()["data"]
        assert "manual_montage" not in proposal.json()["data"]


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"enabled": True, "unknown": 1}])
def test_strict_enabled_contract(lifecycle_client, body):
    http, _, _ = lifecycle_client
    assert http.put("/plugin-packages/acme.device/1.0.0/enabled", json=body).status_code == 422


def test_missing_lifecycle_service_is_unavailable(lifecycle_client):
    http, _, _ = lifecycle_client
    del http.app.state.plugin_package_service
    assert http.delete("/plugin-packages/acme.device/1.0.0").status_code == 503


def test_version_routes_use_configuration_without_a_package_service(lifecycle_client):
    http, packages, profiles = lifecycle_client
    packages.set_enabled("acme.device", "1.0.0", True)
    selection = packages.store.get_package("acme.device", "1.0.0").contributions[0].selection
    source = profiles.create("effector", selection, "Output", {})
    del http.app.state.plugin_package_service
    body = {"source_profile_id": source.profile_id, "target_version": "2.0.0"}
    preview = http.post("/plugin-packages/profile-version-proposals", json=body)
    assert preview.status_code == 200 and not preview.json()["data"]["target_enabled"]
    packages.set_enabled("acme.device", "2.0.0", True)
    saved = http.post("/plugin-packages/version-profiles", json={**body, "expected_source_revision": source.revision})
    assert saved.status_code == 201
    assert saved.json()["data"]["selection"]["version"] == "2.0.0"
    assert saved.json()["data"]["profile_id"] != source.profile_id


@pytest.mark.parametrize("path", ["profile-version-proposals", "version-profiles"])
def test_version_routes_report_missing_configuration(lifecycle_client, path):
    http, _, _ = lifecycle_client
    del http.app.state.container.configuration_service
    body = {"source_profile_id": "source-profile", "target_version": "2.0.0"}
    if path == "version-profiles":
        body["expected_source_revision"] = 1
    response = http.post(f"/plugin-packages/{path}", json=body)
    assert response.status_code == 503
    assert response.json()["code"] == "SERVICE_UNAVAILABLE"
