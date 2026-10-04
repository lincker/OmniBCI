"""Desktop authentication, admission, and consistent session exports."""
import csv
import io
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnibci.domain.repositories import SessionRecord, SessionRecordState, TrialRecord
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
    assert api_client.get("/api/v1/sessions").status_code == 200
    response = api_client.post("/api/v1/sessions")
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


def _record(session_id, *, trials=True):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return SessionRecord(
        session_id, '=HYPERLINK("bad")', SessionRecordState.ENDED, now, now,
        trials=[TrialRecord(1, 3, 101, 0.75, False, now, "trial-revision")] if trials else [],
        config_revision="session-revision",
    )


def test_export_uses_one_complete_repository_snapshot(api_client, session_repository, monkeypatch):
    session_repository.save(_record("first"))
    session_repository.save(_record("empty", trials=False))
    find_all = session_repository.find_all
    calls = []

    def snapshot():
        calls.append(True)
        records = find_all()
        session_repository.delete("first")
        return records

    monkeypatch.setattr(session_repository, "find_all", snapshot)
    response = api_client.get("/api/v1/sessions/export?format=json")
    assert response.status_code == 200
    body = response.json()
    assert body["format_version"] == 1
    assert body["app_version"]
    assert body["exported_at"]
    assert len(calls) == 1
    assert [item["session_id"] for item in body["sessions"]] == ["first", "empty"]
    assert body["sessions"][0]["trial_count"] == len(body["sessions"][0]["trials"]) == 1
    assert body["sessions"][0]["trials"][0]["config_revision"] == "trial-revision"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "no-store"


def test_csv_export_filters_sessions_and_keeps_empty_rows(api_client, session_repository):
    for sid in ("first", "second"):
        session_repository.save(_record(sid))
    session_repository.save(_record("empty", trials=False))
    response = api_client.get("/api/v1/sessions/export?session_id=first&session_id=empty&format=csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [row["session_id"] for row in rows] == ["first", "empty"]
    assert rows[0]["paradigm"].startswith("'=")
    assert rows[0]["session_config_revision"] == "session-revision"
    assert rows[0]["trial_config_revision"] == "trial-revision"
    assert rows[1]["trial_number"] == ""
    assert rows[1]["trial_count"] == "0"


def test_export_rejects_missing_session_and_unknown_format(api_client):
    assert api_client.get("/api/v1/sessions/export?session_id=missing").status_code == 404
    assert api_client.get("/api/v1/sessions/export?format=html").status_code == 422
