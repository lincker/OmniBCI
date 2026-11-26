"""Development uses actual module composition and waits for normal shutdown."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
import urllib.request

import pytest

from omnibci.plugin_development import _start_modules
from tests.plugin_development_helpers import source_project, configuration_file, module_configuration


@pytest.mark.asyncio
async def test_module_startup_uses_current_actions_and_never_replays_reads_or_trials():
    engine = SimpleNamespace(handle_start_engine=AsyncMock())
    runtime = SimpleNamespace(execute_current=AsyncMock(return_value={"status": "succeeded"}))
    modules = module_configuration(["acquisition", "decoder", "paradigm", "effector"])
    await _start_modules(SimpleNamespace(engine_service=engine, runtime_configuration=runtime), modules)
    engine.handle_start_engine.assert_awaited_once()
    calls = runtime.execute_current.await_args_list
    assert [call.args for call in calls] == [
        ("acquisition", "connect"), ("processing", "start"), ("stimulation", "start"), ("actuation", "start"),
    ]
    assert len({call.kwargs["operation_id"] for call in calls}) == 4
    assert [call.kwargs["configuration"] for call in calls] == [modules[name] for name in modules]


@pytest.mark.asyncio
async def test_failed_module_prevents_later_module_startup():
    runtime = SimpleNamespace(execute_current=AsyncMock(return_value={"status": "failed", "error": "probe failed"}))
    container = SimpleNamespace(engine_service=SimpleNamespace(handle_start_engine=AsyncMock()), runtime_configuration=runtime)
    with pytest.raises(RuntimeError, match="probe failed"):
        await _start_modules(container, module_configuration(["acquisition", "decoder"]))
    assert runtime.execute_current.await_count == 1


@pytest.mark.integration
@pytest.mark.parametrize("kinds,target", [(["effector"], "effector"),
                                         (["acquisition", "decoder", "filter", "epoch_detector"], "decoder")])
def test_cli_starts_fresh_real_hosts_and_cleans_up_only_after_shutdown(tmp_path, monkeypatch, kinds, target):
    project = source_project(tmp_path)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        output_port = available.getsockname()[1]
    configuration = configuration_file(tmp_path, kinds, settings={"actuation": {"ACTUATION.server_port": output_port}})
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    (ordinary / "keep.txt").write_text("ordinary inventory")
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(ordinary))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    identities = []
    for iteration in range(2 if target == "effector" else 1):
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        log = tmp_path / f"host-{iteration}.log"
        with log.open("w") as stream:
            process = subprocess.Popen([
                sys.executable, "-B", "-m", "omnibci.plugin_cli", "develop", str(project),
                "--configuration", str(configuration), "--contribution", target, "--port", str(port),
            ], stdout=stream, stderr=subprocess.STDOUT, env=dict(os.environ),
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            try:
                deadline = time.monotonic() + 60
                while True:
                    assert process.poll() is None, log.read_text()
                    ready = list((project / ".omnibci-dev").glob("run-*/ready.json"))
                    if ready:
                        run = ready[0].parent
                        try:
                            with opener.open(f"http://127.0.0.1:{port}/api/v1/engine/status", timeout=1) as response:
                                status = json.load(response)["data"]
                            if target == "effector":
                                with opener.open(f"http://127.0.0.1:{port}/api/v1/actuation/status", timeout=1) as response:
                                    actuation_status = json.load(response)["data"]
                            break
                        except OSError:
                            pass
                    assert time.monotonic() < deadline, log.read_text()
                    time.sleep(.1)
                identities.append(json.loads(ready[0].read_text())["pid"])
                assert status["running"]
                assert actuation_status["active"] if target == "effector" else status["processor_alive"]
                records = [json.loads(path.read_text()) for path in (run / "configuration/execution-history").glob("*.json")]
                assert records, log.read_text()
                assert '"origin": "development"' in json.dumps(records)
                expected = "first" if iteration == 0 else "second"
                snapshot = run / "plugins/packages/test.device/1.0.0/value.txt"
                assert snapshot.read_text() == expected
                (project / "value.txt").write_text("second")
                assert snapshot.read_text() == expected
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                assert process.wait(timeout=45) == 0, log.read_text()
                assert not run.exists()
                assert not (project / ".omnibci-dev/state").exists()
            finally:
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                    try:
                        process.wait(timeout=45)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        assert (ordinary / "keep.txt").read_text() == "ordinary inventory"
        assert sorted(path.name for path in ordinary.iterdir()) == ["keep.txt"]
    assert len(set(identities)) == len(identities)
