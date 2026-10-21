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
from urllib.request import Request, urlopen

import pytest

from omnibci.presentation.server import _bootstrap


@pytest.mark.parametrize("message", [None, {}, {"op": "bootstrap", "token": "short", "launch_id": "test"}])
def test_invalid_bootstrap_never_starts_a_backend(message):
    import io
    content = "" if message is None else json.dumps(message) + "\n"
    with pytest.raises(ValueError):
        _bootstrap(io.StringIO(content))


@pytest.fixture
def desktop_backend(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir()
    (directory / "settings.toml").write_text(
        "[LOGGING]\nfile_path = " + json.dumps(str(tmp_path / "backend.log")) + "\n", encoding="utf-8",
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
                    pytest.fail("Backend control timed out: " + log.read())
                if result is None:
                    log.seek(0)
                    pytest.fail("Backend exited before replying: " + log.read())
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


def request(ready, path, *, method="GET"):
    return urlopen(Request(ready["url"] + path, method=method,
                          headers={"Authorization": "Bearer " + "t" * 48}), timeout=5)


@pytest.mark.integration
def test_desktop_handshake_prepare_resume_and_clean_shutdown(desktop_backend):
    process, send, receive, ready = desktop_backend
    with request(ready, "/api/v1/system/info") as response:
        assert json.load(response)["data"]["instance_id"] == ready["instance_id"]
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
    send("shutdown", id="exit")
    assert receive() == {"event": "result", "id": "exit", "ok": True, "errors": []}
    assert process.wait(timeout=15) == 0


@pytest.mark.integration
def test_parent_pipe_eof_stops_backend(desktop_backend):
    process, _, _, _ = desktop_backend
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
