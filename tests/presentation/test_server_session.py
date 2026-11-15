"""Browser sessions and desktop credentials share one Server without owning it."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
