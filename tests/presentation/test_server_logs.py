"""The log endpoint exposes only the current server's bounded file tail."""

import pytest
from fastapi.testclient import TestClient

from omnibci.infrastructure.server_logging import ServerLogging
from omnibci.presentation.main import create_app


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
