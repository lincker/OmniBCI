"""Tests for /api/v1/server endpoints (info, session, desktop auth, request gate, and logs)."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnibci.infrastructure.server_logging import ServerLogging
from omnibci.presentation.main import create_app


TOKEN = {"Authorization": "Bearer private-token"}
ORIGIN = {"Origin": "http://testserver"}
SESSION = "/api/v1/server/session"
INFO = "/api/v1/server/info"
RESULTS = "/api/v1/ws/results"


def login(client):
    response = client.post(SESSION, headers={**TOKEN, **ORIGIN})
    assert response.status_code == 200
    return response


def test_browser_login_shares_desktop_identity_without_exposing_api_token():
    app = create_app(api_token="private-token")
    browser, desktop = TestClient(app), TestClient(app)
    assert browser.get(INFO).status_code == 401
    assert browser.post(SESSION, headers={**ORIGIN, "Authorization": "Bearer wrong"}).status_code == 401
    response = login(browser)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/api/v1" in cookie
    assert "private-token" not in cookie + response.text
    assert "Max-Age" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert browser.get(INFO).json() == desktop.get(INFO, headers=TOKEN).json() == response.json()
    assert response.json()["data"]["desktop_api_version"] == 1


def test_session_creation_requires_bearer_even_with_an_existing_browser_session():
    browser = TestClient(create_app(api_token="private-token"))
    login(browser)
    assert browser.post(SESSION, headers=ORIGIN).status_code == 401
    assert browser.get(INFO, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert browser.post(SESSION, headers={**ORIGIN, "Authorization": "Bearer wrong"}).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with browser.websocket_connect(RESULTS, headers={**ORIGIN, "Authorization": "Bearer wrong"}):
            pass


def test_logout_clears_only_this_browser_and_does_not_prepare_server_shutdown():
    app = create_app(api_token="private-token")
    browser, other_browser, desktop = TestClient(app), TestClient(app), TestClient(app)
    login(browser)
    login(other_browser)
    response = browser.delete(SESSION, headers=ORIGIN)
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert browser.get(INFO).status_code == 401
    assert other_browser.get(INFO).status_code == 200
    assert desktop.get(INFO, headers=TOKEN).status_code == 200
    assert not app.state.request_gate.prepared


@pytest.mark.parametrize("origin", [
    "http://foreign.example", "http://testserver.evil", "null",
    "https://testserver", "http://testserver:8000", "http://testserver/path",
])
def test_foreign_origins_cannot_use_or_create_browser_sessions(origin):
    browser = TestClient(create_app(api_token="private-token"))
    login(browser)
    response = browser.post(SESSION, headers={**TOKEN, "Origin": origin})
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
    assert "access-control-allow-origin" not in response.headers
    assert browser.get(INFO, headers={"Origin": origin}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with browser.websocket_connect(RESULTS, headers={"Origin": origin}):
            pass


def test_cookie_writes_require_same_origin_but_native_bearer_requests_do_not():
    app = create_app(api_token="private-token")
    writes = []

    @app.post("/api/v1/test-write")
    def write():
        writes.append(True)
        return {"ok": True}

    browser = TestClient(app)
    login(browser)
    assert browser.post("/api/v1/test-write").status_code == 403
    assert browser.post("/api/v1/test-write", headers=ORIGIN).status_code == 200
    assert browser.post("/api/v1/test-write", headers=TOKEN).status_code == 200
    assert len(writes) == 2


def test_browser_and_desktop_receive_results_and_disconnect_independently():
    from omnibci.presentation.websockets.results import results_broadcaster

    app = create_app(api_token="private-token")
    with TestClient(app) as browser:
        login(browser)
        with pytest.raises(WebSocketDisconnect):
            with browser.websocket_connect(RESULTS):
                pass
        with browser.websocket_connect(RESULTS, headers=TOKEN) as desktop_socket:
            with browser.websocket_connect(RESULTS, headers=ORIGIN) as browser_socket:
                browser.portal.call(results_broadcaster._broadcast, '{"event":"Ping"}')
                assert desktop_socket.receive_json() == browser_socket.receive_json() == {"event": "Ping"}
            browser.portal.call(results_broadcaster._broadcast, '{"event":"Ping"}')
            assert desktop_socket.receive_json() == {"event": "Ping"}
            assert browser.get(INFO, headers=TOKEN).status_code == 200
            assert not app.state.request_gate.prepared


def test_session_cookie_is_secure_over_https_and_default_ports_match():
    browser = TestClient(create_app(api_token="private-token"), base_url="https://testserver")
    response = browser.post(SESSION, headers={**TOKEN, "Origin": "https://testserver:443"})
    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]
    assert browser.get(INFO).status_code == 200
    with browser.websocket_connect("wss://testserver/api/v1/ws/results", headers={"Origin": "https://testserver"}):
        pass


def test_browser_session_does_not_survive_server_restart_with_same_api_token():
    first = TestClient(create_app(api_token="private-token"))
    login(first)
    restarted = TestClient(create_app(api_token="private-token"))
    restarted.cookies.update(first.cookies)
    assert restarted.get(INFO).status_code == 401
    assert restarted.get(INFO, headers=TOKEN).status_code == 200


def test_unauthenticated_server_supports_both_clients_without_creating_cookie():
    client = TestClient(create_app(api_token=""))
    assert client.get(INFO).json()["data"]["auth_enabled"] is False
    response = client.post(SESSION, headers=ORIGIN)
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    with client.websocket_connect(RESULTS, headers=ORIGIN):
        pass
    assert client.get(INFO, headers={"Origin": "http://foreign.example"}).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(RESULTS, headers={"Origin": "http://foreign.example"}):
            pass


def test_token_protects_rest_and_websocket_without_query_credentials():
    app = create_app(api_token="private-token")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            assert client.get("/api/v1/server/info", headers=headers).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/ws/results?token=private-token"):
                pass
        with client.websocket_connect(
            "/api/v1/ws/results", headers={"Authorization": "Bearer private-token"},
        ):
            pass
        info = client.get("/api/v1/server/info", headers={"Authorization": "Bearer private-token"}).json()["data"]
        assert info["desktop_api_version"] == 1
        assert info["auth_enabled"] is True
        assert info["instance_id"] == app.state.instance_id
        assert "private-token" not in str(info)


def test_environment_token_and_distinct_server_instances(monkeypatch):
    monkeypatch.setenv("OMNIBCI_API_TOKEN", "environment-secret")
    first, second = create_app(), create_app()
    assert first.state.instance_id != second.state.instance_id
    client = TestClient(first)
    assert client.get("/api/v1/server/info").status_code == 401
    assert client.get("/api/v1/server/info", headers={"Authorization": "Bearer environment-secret"}).status_code == 200


def test_prepared_server_allows_reads_but_rejects_mutations(api_client):
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
        assert (await client.get("/api/v1/server/info")).status_code == 200
        released.set()
        assert (await accepted).status_code == 200
        await preparing


@pytest.fixture
def log_client(tmp_path):
    app = create_app(api_token="")
    service = ServerLogging({"LOGGING.file_path": str(tmp_path / "private" / "server.log")})
    service.file_path.parent.mkdir()
    app.state.server_logging = service
    return TestClient(app), service


def test_logs_return_only_current_file_and_disable_caching(log_client, tmp_path):
    client, service = log_client
    service.file_path.write_text("current record\n", encoding="utf-8")
    other = tmp_path / "other.log"
    other.write_text("unrelated secret", encoding="utf-8")
    response = client.get("/api/v1/server/logs", params={"path": str(other)})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == {
        "filename": service.file_path.name,
        "lines": ["current record"],
        "truncated": False,
    }
    assert str(tmp_path) not in response.text
    assert "unrelated secret" not in response.text


def test_logs_empty_file_is_a_successful_empty_snapshot(log_client):
    client, service = log_client
    service.file_path.touch()
    response = client.get("/api/v1/server/logs")
    assert response.status_code == 200
    assert response.json()["data"]["lines"] == []
    assert response.json()["data"]["truncated"] is False


@pytest.mark.parametrize("unavailable", ["not_started", "missing_file", "read_error"])
def test_logs_unavailable_has_safe_service_error(log_client, unavailable, monkeypatch, tmp_path):
    client, service = log_client
    if unavailable == "not_started":
        del client.app.state.server_logging
    elif unavailable == "read_error":
        def fail():
            raise PermissionError(f"Cannot read private file {service.file_path}")
        monkeypatch.setattr(service, "read_tail", fail)
    response = client.get("/api/v1/server/logs")
    assert response.status_code == 503
    assert response.json() == {
        "success": False, "code": "SERVICE_UNAVAILABLE",
        "message": "Server logs are unavailable", "detail": None,
    }
    assert str(tmp_path) not in response.text


def test_logs_require_api_token_before_reading(log_client, monkeypatch):
    _, service = log_client
    app = create_app(api_token="private-token")
    app.state.server_logging = service
    reads = []

    def read_tail():
        reads.append(True)
        return ["authorized record"], False

    monkeypatch.setattr(service, "read_tail", read_tail)
    client = TestClient(app)
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        assert client.get("/api/v1/server/logs?token=private-token", headers=headers).status_code == 401
    assert reads == []
    response = client.get("/api/v1/server/logs", headers={"Authorization": "Bearer private-token"})
    assert response.status_code == 200
    assert response.json()["data"]["lines"] == ["authorized record"]
    assert len(reads) == 1
