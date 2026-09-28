"""Real bounded ZIP upload through the installation-only HTTP adapter."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.application.plugin_profiles import PluginProfileStorageError, PluginProfileValidationError, ProfileRevisionConflict
from omnibci.application.services.plugin_installation_service import PluginInstallationService
from omnibci.infrastructure.plugins.package_store import DirectoryPluginPackageStore
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_packages import router
from tests.infrastructure.test_package_store import package_archive


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    service = PluginInstallationService(DirectoryPluginPackageStore())
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.plugin_installation_service = service
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
    del http.app.state.plugin_installation_service
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
