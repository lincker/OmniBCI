"""Desktop authentication and request admission."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnibci.presentation.main import create_app


def test_token_protects_rest_and_websocket_without_query_credentials():
    app = create_app(api_token="private-token")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            assert client.get("/api/v1/system/info", headers=headers).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws/results?token=private-token"):
                pass
        with client.websocket_connect(
            "/api/v1/ws/results", headers={"Authorization": "Bearer private-token"},
        ):
            pass
        info = client.get("/api/v1/system/info", headers={"Authorization": "Bearer private-token"}).json()["data"]
        assert info["desktop_api_version"] == 1
        assert info["auth_enabled"] is True
        assert info["instance_id"] == app.state.instance_id
        assert "private-token" not in str(info)


def test_environment_token_and_distinct_backend_instances(monkeypatch):
    monkeypatch.setenv("OMNIBCI_API_TOKEN", "environment-secret")
    first, second = create_app(), create_app()
    assert first.state.instance_id != second.state.instance_id
    client = TestClient(first)
    assert client.get("/api/v1/system/info").status_code == 401
    assert client.get("/api/v1/system/info", headers={"Authorization": "Bearer environment-secret"}).status_code == 200


def test_prepared_backend_allows_reads_but_rejects_mutations(api_client):
    api_client.app.state.request_gate.prepared = True
    assert api_client.get("/api/v1/results").status_code == 200
    response = api_client.post("/api/v1/acquisition/stream/start")
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


@pytest.mark.asyncio
async def test_prepare_waits_for_accepted_write_and_blocks_later_writes():
    import httpx

    app = create_app(api_token="")
    started, released = asyncio.Event(), asyncio.Event()

    @app.post("/api/v1/test-write")
    async def write():
        started.set()
        await released.wait()
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        accepted = asyncio.create_task(client.post("/api/v1/test-write"))
        await started.wait()
        preparing = asyncio.create_task(app.state.request_gate.prepare())
        await asyncio.sleep(0)
        assert not preparing.done()
        assert (await client.post("/api/v1/test-write")).status_code == 409
        assert (await client.get("/api/v1/system/info")).status_code == 200
        released.set()
        assert (await accepted).status_code == 200
        await preparing
