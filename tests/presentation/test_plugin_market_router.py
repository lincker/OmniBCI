"""Market HTTP responses, signature failures and nonblocking repository calls."""
import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.plugin_market import router
from tests.infrastructure.test_market_adapter import configured
from tests.infrastructure.test_market_repository import _URL, _Response


@pytest.fixture
def app(configured):
    service, _, _, _, _ = configured
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.plugin_market_service = service
    @app.get("/health")
    def health():
        return {"ok": True}
    return app


def test_signed_catalog_and_install_keep_package_disabled(app):
    with TestClient(app) as http:
        catalog = http.get("/market/catalog")
        assert catalog.status_code == 200, catalog.text
        item = catalog.json()["data"][0]
        assert item["repository_id"] == "example.plugins"
        assert item["contributions"][0]["selection"] == {"package_id": "acme.device", "contribution_id": "output", "version": "1.0.0"}
        installed = http.post("/market/install", json={"package_id": "acme.device", "version": "1.0.0"})
        assert installed.status_code == 201, installed.text
        assert not installed.json()["data"]["package"]["enabled"]
        assert not installed.json()["data"]["already_installed"]


def test_disabled_market_is_explicitly_unavailable(app):
    app.state.plugin_market_service = None
    with TestClient(app) as http:
        assert http.get("/market/catalog").status_code == 503
        assert http.post("/market/install", json={"package_id": "acme.device", "version": "1.0.0"}).status_code == 503


def test_request_cannot_override_repository_keys_or_download_url(app):
    with TestClient(app) as http:
        response = http.post("/market/install", json={
            "package_id": "acme.device", "version": "1.0.0", "download_url": "https://untrusted.example/package.zip",
        })
        assert response.status_code == 422
        assert http.post("/market/install", json={"package_id": "acme.device", "version": "latest"}).status_code == 422


def test_invalid_signature_never_becomes_catalog_or_installation(app):
    repository = app.state.plugin_market_service.repository.repository
    raw = repository._opener.routes[_URL]
    repository._opener.routes[_URL] = raw.replace(b"Example Reader", b"Forged Reader")
    with TestClient(app) as http:
        assert http.get("/market/catalog").status_code == 422
        assert http.post("/market/install", json={"package_id": "acme.device", "version": "1.0.0"}).status_code == 422
        assert not app.state.plugin_market_service.repository.store.list_packages()


@pytest.mark.asyncio
async def test_repository_network_work_does_not_block_event_loop(app):
    repository = app.state.plugin_market_service.repository.repository
    raw = repository._opener.routes[_URL]
    started, release = threading.Event(), threading.Event()
    def delayed(url):
        started.set()
        release.wait(timeout=3)
        return _Response(url, raw)
    repository._opener.routes[_URL] = delayed
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        request = asyncio.create_task(client.get("/market/catalog"))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            health = await client.get("/health")
            assert health.status_code == 200 and not request.done()
        finally:
            release.set()
        assert (await request).status_code == 200
