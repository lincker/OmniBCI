"""Exercise the installed-style entry point and its private control boundary."""
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import asyncio
import io
from contextlib import asynccontextmanager
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from omnibci.presentation.server import _bootstrap


@pytest.mark.parametrize("message", [None, {}, {"op": "bootstrap", "token": "short", "launch_id": "test"}])
def test_invalid_bootstrap_never_starts_a_server(message):
    import io
    content = "" if message is None else json.dumps(message) + "\n"
    with pytest.raises(ValueError):
        _bootstrap(io.StringIO(content))


@pytest.fixture
def desktop_server(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "settings.toml").write_text(
        "[LOGGING]\nfile_path = " + json.dumps(str(tmp_path / "server.log")) + "\n", encoding="utf-8",
    )
    environment = {**os.environ, "OMNIBCI_CONFIG_DIR": str(directory),
                   "OMNIBCI_PLUGIN_DIR": str(tmp_path / "plugins")}
    with (tmp_path / "stderr.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "omnibci.presentation.server", "--desktop"],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
            text=True, encoding="utf-8", start_new_session=os.name != "nt",
        )
        output = queue.Queue()

        def read():
            for line in process.stdout:
                output.put(json.loads(line))
            output.put(None)

        threading.Thread(target=read, daemon=True).start()

        def send(op, **values):
            process.stdin.write(json.dumps({"op": op, **values}) + "\n")
            process.stdin.flush()

        def receive():
            while True:
                try:
                    result = output.get(timeout=30)
                except queue.Empty:
                    log.seek(0)
                    pytest.fail("Server control timed out: " + log.read())
                if result is None:
                    log.seek(0)
                    pytest.fail("Server exited before replying: " + log.read())
                if result["event"] == "shutdown_progress":
                    assert isinstance(result["phase"], str)
                    continue
                return result

        try:
            send("bootstrap", token="t" * 48, launch_id="test-launch")
            ready = receive()
            assert ready["event"] == "ready"
            assert ready["launch_id"] == "test-launch"
            assert ready["url"].startswith("http://127.0.0.1:")
            yield process, send, receive, ready
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            process.stdout.close()


def request(ready, path, *, method="GET", body=None):
    # The private child is always on loopback, regardless of host proxy settings.
    return build_opener(ProxyHandler({})).open(
        Request(ready["url"] + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + "t" * 48, "Content-Type": "application/json"}), timeout=5,
    )


@pytest.mark.integration
def test_desktop_handshake_prepare_resume_and_clean_shutdown(desktop_server, unused_tcp_port):
    from uuid import uuid4
    from tests.plugin_configuration_helpers import builtin_selection
    process, send, receive, ready = desktop_server
    with request(ready, "/api/v1/server/info") as response:
        assert json.load(response)["data"]["instance_id"] == ready["instance_id"]
    configuration = {"expected_revision": None, "settings": {"STIMULATION.auto_spawn": False,
                     "STIMULATION.server_port": unused_tcp_port}, "bindings": [{"slot": "stimulation.paradigm",
                     "selection": builtin_selection("paradigm", "STIMULATION_THUBCI_SSVEP_classic40").to_dict(),
                     "parameters": {}}]}
    with request(ready, "/api/v1/stimulation/start", method="POST", body={
        "operation_id": str(uuid4()), "configuration": configuration,
    }) as response:
        assert json.load(response)["data"]["result"]["action"] == "prepared"
    with request(ready, "/api/v1/config/runtime") as response:
        first = json.load(response)["data"]["stimulation_session"]
    send("prepare_shutdown", id="prepare")
    assert receive() == {"event": "result", "id": "prepare", "ok": True, "errors": []}
    with request(ready, "/api/v1/results") as response:
        assert json.load(response)["data"] == {"generation": None, "trials": []}
    with pytest.raises(HTTPError) as blocked:
        request(ready, "/api/v1/missing", method="POST")
    assert blocked.value.code == 409
    blocked.value.close()
    send("resume", id="resume")
    assert receive()["ok"] is True
    with pytest.raises(HTTPError) as admitted:
        request(ready, "/api/v1/missing", method="POST")
    assert admitted.value.code == 404
    admitted.value.close()
    with request(ready, "/api/v1/config/runtime") as response:
        assert json.load(response)["data"]["stimulation_state"] == "stopped"
    with request(ready, "/api/v1/stimulation/prepare", method="POST") as response:
        assert response.status == 200
    with request(ready, "/api/v1/config/runtime") as response:
        state = json.load(response)["data"]
        assert state["stimulation_session"] not in (None, first) and not state["running"]
    send("shutdown", id="exit")
    assert receive() == {"event": "result", "id": "exit", "ok": True, "errors": []}
    assert process.wait(timeout=15) == 0


@pytest.mark.integration
def test_parent_pipe_eof_stops_server(desktop_server):
    process, _, _, _ = desktop_server
    process.stdin.close()
    assert process.wait(timeout=15) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_late_lifespan_failure_is_not_acknowledged_as_clean_shutdown(monkeypatch):
    from fastapi import FastAPI
    from omnibci.presentation import main, server

    @asynccontextmanager
    async def lifespan(app):
        yield
        raise RuntimeError("late cleanup failure")

    app = FastAPI(lifespan=lifespan)
    app.state.instance_id = "test-instance"
    app.state.shutdown_errors = []
    app.state.shutdown_complete = False
    monkeypatch.setattr(main, "create_app", lambda **_: app)
    incoming = queue.Queue()
    emitted = []

    class Input:
        def readline(self, _):
            return incoming.get(timeout=10)

    class Output(io.StringIO):
        def write(self, value):
            message = json.loads(value)
            emitted.append(message)
            if message["event"] == "ready":
                incoming.put('{"op":"shutdown","id":"exit"}\n')
            return super().write(value)

    try:
        await asyncio.wait_for(server._desktop_server("t" * 48, "test", Input(), Output()), timeout=10)
    finally:
        incoming.put("")
    result = next(message for message in emitted if message["event"] == "result")
    assert result["ok"] is False
    assert result["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_lifespan_preserves_ipc_startup_error_and_retries_only_retained_resources(monkeypatch, rollback_fails):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from omnibci.presentation import main

    ipc = MagicMock()
    ipc.has_owned_resources = rollback_fails
    ipc.start.side_effect = RuntimeError("ipc startup boom")
    if rollback_fails:
        ipc.stop.side_effect = OSError("outer stop retry failed")
    package_owner = MagicMock()
    container = SimpleNamespace(
        engine_service=SimpleNamespace(is_engine_ready=False, is_initialized=False),
        acquisition_service=SimpleNamespace(),
        stimulation_service=SimpleNamespace(status=lambda _: {}),
        actuation_service=SimpleNamespace(),
        runtime_configuration=None,
        plugin_package_service=None,
        plugin_market_service=None,
        ipc_runtime=ipc,
        package_owner=package_owner,
    )
    shutdown_prepare = AsyncMock(return_value=[])
    monkeypatch.setattr(main, "build_container", AsyncMock(return_value=container))
    monkeypatch.setattr(main, "ServerShutdownService", lambda **_: SimpleNamespace(prepare=shutdown_prepare))

    app = main.create_app()
    with pytest.raises(RuntimeError, match="ipc startup boom") as exc_info:
        async with main._services_lifespan(app, {}):
            pass

    shutdown_prepare.assert_awaited_once()
    if rollback_fails:
        package_owner.abandon.assert_called_once()
        package_owner.close.assert_not_called()
        ipc.stop.assert_called_once()
        assert app.state.shutdown_errors == ["IPC runtime: outer stop retry failed"]
        assert any("outer stop retry failed" in note for note in getattr(exc_info.value, "__notes__", ()))
    else:
        package_owner.close.assert_called_once()
        package_owner.abandon.assert_not_called()
        ipc.stop.assert_not_called()
        assert app.state.shutdown_errors == []


@pytest.mark.asyncio
async def test_lifespan_runs_full_teardown_when_status_publisher_task_fails(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from omnibci.presentation import main

    ipc = MagicMock()
    ipc.has_owned_resources = True
    package_owner = MagicMock()
    container = SimpleNamespace(
        engine_service=SimpleNamespace(is_engine_ready=False, is_initialized=False),
        acquisition_service=SimpleNamespace(),
        stimulation_service=SimpleNamespace(status=lambda _: {}),
        actuation_service=SimpleNamespace(),
        runtime_configuration=None,
        plugin_package_service=None,
        plugin_market_service=None,
        ipc_runtime=ipc,
        package_owner=package_owner,
    )
    shutdown_prepare = AsyncMock(return_value=[])

    async def failing_publisher(_):
        raise RuntimeError("publisher died")

    monkeypatch.setattr(main, "build_container", AsyncMock(return_value=container))
    monkeypatch.setattr(main, "ServerShutdownService", lambda **_: SimpleNamespace(prepare=shutdown_prepare))
    monkeypatch.setattr(main.results_broadcaster, "publish_stimulation_status", failing_publisher)

    app = main.create_app()
    with pytest.raises(RuntimeError, match="Status publisher: publisher died"):
        async with main._services_lifespan(app, {}):
            await asyncio.sleep(0)

    shutdown_prepare.assert_awaited_once()
    ipc.stop.assert_called_once()
    package_owner.abandon.assert_called_once()
    assert app.state.shutdown_errors == ["Status publisher: publisher died"]
