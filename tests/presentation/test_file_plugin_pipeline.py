"""Real module hosts preserve identity from file samples to output completion."""
import json
from pathlib import Path
import socket
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from omnibci.application.configuration.definitions import MODULE_SETTING_DEFAULTS
from omnibci.infrastructure.plugin_management.store import ManagedPluginPackageStore
from omnibci.infrastructure.plugin_management.project_tools import package_project
from omnibci.presentation.main import create_app
from omnibci.shared.events import EventType


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_file_starter_decoder_and_logger_preserve_trial_and_execution_identity(tmp_path, monkeypatch):
    configuration_root = tmp_path / "configuration"
    configuration_root.mkdir()
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(configuration_root))
    # Allow cold MNE/Matplotlib imports in the spawned file reader on CI.
    (configuration_root / "settings.toml").write_text(
        '[LOGGING]\nfile_path=' + json.dumps(str(tmp_path / "host.log"))
        + '\n[NEUROENGINE]\ncommand_timeout_s=30.0\n', encoding="utf-8",
    )
    archive = tmp_path / "starter.zip"
    package_project(ROOT / "examples/decoder_plugin", archive)
    store = ManagedPluginPackageStore()
    installed = store.install_archive(archive).package
    store.set_enabled(installed.package_id, installed.version, True)

    from tests.plugin_configuration_helpers import builtin_selection

    def binding(slot, kind, alias, parameters, **extra):
        descriptor = next(
            (item for item in store.catalog.list_contributions(kind) if item.contribution_id == alias),
            None,
        ) or store.catalog.resolve_selection(kind, builtin_selection(kind, alias))
        return {"slot": slot, "selection": descriptor.selection.to_dict(), "parameters": parameters, **extra}

    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    configurations = {
        "acquisition": {"expected_revision": None, "settings": {}, "bindings": [
            binding("acquisition.source", "acquisition", "file_reader", {
                "data_file_path": str(ROOT / "tests/fixture/train.cnt"), "file_reader_chunk_size": 1000,
            })]},
        "processing": {"expected_revision": None, "settings": dict(MODULE_SETTING_DEFAULTS["processing"]), "bindings": [
            binding("processing.decoder", "decoder", "oz-polarity", {"window_duration": .1},
                    processing_input={"schema_version": 2, "outputs": []}),
            binding("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {})]},
        "actuation": {"expected_revision": None, "settings": {
            **MODULE_SETTING_DEFAULTS["actuation"], "ACTUATION.server_port": port,
        }, "bindings": [binding("actuation.effector", "effector", "logger", {})]},
    }
    received, acknowledgements, trials = Event(), [], []

    def acknowledged(event):
        if event.data.get("status") == "ok":
            acknowledgements.append(event.data)
            received.set()

    app = create_app()
    with TestClient(app) as client:
        engine = app.state.container.engine_service
        engine.subscribe(EventType.TrialCompleted, lambda event: trials.append(event.data))
        client.app.state.container.ipc_runtime.subscribe(EventType.ActuationAck, acknowledged)
        for module, configuration in configurations.items():
            path = "/acquisition/connect" if module == "acquisition" else f"/{module}/start"
            response = client.post("/api/v1" + path, json={
                "operation_id": str(uuid4()), "configuration": configuration,
            })
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "succeeded", response.text
        connection = client.get("/api/v1/acquisition/status").json()["data"]["connection_id"]
        runtime = client.get("/api/v1/config/runtime").json()["data"]
        response = client.post("/api/v1/acquisition/stream/start", json={
            "expected_revision": runtime["applied"]["acquisition"],
            "generation": runtime["generation"], "connection_id": connection,
        })
        assert response.status_code == 200, response.text
        assert received.wait(20), "No successful Logger acknowledgement"
        assert client.post("/api/v1/acquisition/stream/stop").status_code == 200
        acknowledgement = acknowledgements[0]
        trial = next(item for item in trials if item.trial_number == acknowledgement["origin"]["trial_number"])
        assert acknowledgement["origin"]["generation"] == trial.generation
        assert acknowledgement["execution"]["generation"] == runtime["actuation_session"]
        assert runtime["actuation_session"] != trial.generation
        assert acknowledgement["origin"]["processing_revision"] == trial.processing_revision
        assert acknowledgement["origin"]["acquisition_revision"] == trial.acquisition_revision
        assert acknowledgement["target_id"] == trial.classification.target_id
        receipts = [json.loads(path.read_text(encoding="utf-8"))["receipt"]
                    for path in (configuration_root / "execution-history").glob("*.json")]
        assert {item["module"] for item in receipts} == {"acquisition", "processing", "actuation"}
        assert any(all(item[key] == value for key, value in acknowledgement["execution"].items()) for item in receipts)
        visible = client.get("/api/v1/results").json()["data"]
        assert visible["trials"] and visible["generation"] == trial.generation
    assert app.state.shutdown_complete
    assert not (tmp_path / "plugins/state/.controller.active").exists()


def _cnt_cca_configurations(catalog, *, chunk_size=100):
    from tests.plugin_configuration_helpers import builtin_selection

    def binding(slot, kind, alias, parameters, **extra):
        descriptor = catalog.resolve_selection(kind, builtin_selection(kind, alias))
        return {"slot": slot, "selection": descriptor.selection.to_dict(), "parameters": parameters, **extra}

    return {
        "acquisition": {"expected_revision": None, "settings": {}, "bindings": [
            binding("acquisition.source", "acquisition", "file_reader", {
                "data_file_path": str(ROOT / "tests/fixture/train.cnt"),
                "file_reader_chunk_size": chunk_size,
            }),
        ]},
        "processing": {"expected_revision": None, "settings": dict(MODULE_SETTING_DEFAULTS["processing"]), "bindings": [
            binding("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA",
                    {"window_duration": 3.0, "offset_time": 0.14},
                    processing_input={"schema_version": 2, "outputs": []}),
            binding("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ]},
    }


def _wait_for_trials(client, *, minimum=1, timeout_s=30.0):
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        trials = client.get("/api/v1/results").json()["data"]["trials"]
        if len(trials) >= minimum:
            return trials
        time.sleep(0.1)
    raise AssertionError(f"Expected at least {minimum} decoded trials within {timeout_s}s")


@pytest.mark.integration
def test_cnt_replay_serial_and_raced_stop_sequences_exit_cleanly(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    configuration_root = tmp_path / "configuration"
    configuration_root.mkdir()
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(configuration_root))
    (configuration_root / "settings.toml").write_text(
        "[LOGGING]\nfile_path=" + json.dumps(str(tmp_path / "host.log"))
        + "\n[NEUROENGINE]\ncommand_timeout_s=30.0\n",
        encoding="utf-8",
    )
    store = ManagedPluginPackageStore()
    configs = _cnt_cca_configurations(store.catalog, chunk_size=100)

    def start_replay(client):
        assert client.post("/api/v1/engine/start").status_code == 200
        for module, path in (("acquisition", "/acquisition/connect"), ("processing", "/processing/start")):
            revision = client.get(f"/api/v1/config/modules/{module}").json()["data"]["revision"]
            response = client.post("/api/v1" + path, json={
                "operation_id": str(uuid4()),
                "configuration": {**configs[module], "expected_revision": revision},
            })
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "succeeded", response.text
        connection = client.get("/api/v1/acquisition/status").json()["data"]["connection_id"]
        runtime = client.get("/api/v1/config/runtime").json()["data"]
        response = client.post("/api/v1/acquisition/stream/start", json={
            "expected_revision": runtime["applied"]["acquisition"],
            "generation": runtime["generation"],
            "connection_id": connection,
        })
        assert response.status_code == 200, response.text
        _wait_for_trials(client, minimum=1)
        return runtime["generation"]

    app = create_app(desktop=True)
    with TestClient(app) as client:
        container = app.state.container
        # Ordering A: serial stream stop -> processing stop -> engine stop.
        gen1 = start_replay(client)
        assert client.post("/api/v1/acquisition/stream/stop").status_code == 200
        assert client.post("/api/v1/processing/stop").status_code == 200
        engine1 = container.engine_service._engine
        assert client.post("/api/v1/engine/stop").status_code == 200
        assert engine1.shutdown_complete is True
        assert not container.engine_service.restart_blocked
        assert not container.engine_service.admission.recovery_required

        # Ordering B: concurrent processing stop racing an in-flight stream stop.
        gen2 = start_replay(client)
        assert gen2 != gen1
        drain_entered = Event()
        original_drain = container.acquisition_service.handle_stop_and_drain

        def instrumented_drain(*args, **kwargs):
            drain_entered.set()
            return original_drain(*args, **kwargs)

        monkeypatch.setattr(container.acquisition_service, "handle_stop_and_drain", instrumented_drain)
        with ThreadPoolExecutor(max_workers=2) as pool:
            stream_stop_future = pool.submit(client.post, "/api/v1/acquisition/stream/stop")
            assert drain_entered.wait(10), "StopAndDrain did not start"
            processing_stop_future = pool.submit(client.post, "/api/v1/processing/stop")
            stream_resp = stream_stop_future.result(timeout=30)
            proc_resp = processing_stop_future.result(timeout=30)
        monkeypatch.setattr(container.acquisition_service, "handle_stop_and_drain", original_drain)

        assert stream_resp.status_code == 200, stream_resp.text
        assert proc_resp.status_code == 200, proc_resp.text
        engine2 = container.engine_service._engine
        assert client.post("/api/v1/engine/stop").status_code == 200
        assert engine2.shutdown_complete is True
        assert not container.engine_service.restart_blocked
        assert not container.engine_service.admission.recovery_required

        # A third Engine generation starts cleanly after the raced teardown.
        assert client.post("/api/v1/engine/start").status_code == 200
        assert client.get("/api/v1/config/runtime").json()["data"]["generation"] not in (gen1, gen2)
        assert client.post("/api/v1/engine/stop").status_code == 200
    assert app.state.shutdown_complete


@pytest.mark.integration
def test_cnt_replay_failed_detach_drains_abandoned_queue_before_processing_restart(tmp_path, monkeypatch):
    from omnibci.application.services.command_result_service import CommandTimeoutError

    configuration_root = tmp_path / "configuration"
    configuration_root.mkdir()
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(configuration_root))
    (configuration_root / "settings.toml").write_text(
        "[LOGGING]\nfile_path=" + json.dumps(str(tmp_path / "host.log"))
        + "\n[NEUROENGINE]\ncommand_timeout_s=30.0\n",
        encoding="utf-8",
    )
    store = ManagedPluginPackageStore()
    configs = _cnt_cca_configurations(store.catalog, chunk_size=100)

    app = create_app(desktop=True)
    with TestClient(app) as client:
        container = app.state.container
        assert client.post("/api/v1/engine/start").status_code == 200
        for module, path in (("acquisition", "/acquisition/connect"), ("processing", "/processing/start")):
            response = client.post("/api/v1" + path, json={
                "operation_id": str(uuid4()), "configuration": configs[module],
            })
            assert response.status_code == 200, response.text
            assert response.json()["data"]["status"] == "succeeded", response.text
        connection = client.get("/api/v1/acquisition/status").json()["data"]["connection_id"]
        runtime = client.get("/api/v1/config/runtime").json()["data"]
        assert client.post("/api/v1/acquisition/stream/start", json={
            "expected_revision": runtime["applied"]["acquisition"],
            "generation": runtime["generation"],
            "connection_id": connection,
        }).status_code == 200
        initial_trials = _wait_for_trials(client, minimum=1)

        # Simulate a detach timeout while Acquisition continues reading so
        # Processing stops with an unconfirmed boundary and must flush the FIFO.
        original_detach = container.signal_setup_service.detach
        failed_once = {"done": False}

        def fail_detach_once(*args, **kwargs):
            if not failed_once["done"]:
                failed_once["done"] = True
                raise CommandTimeoutError("forced-detach-timeout", "DetachProcessingInput", 0.05)
            return original_detach(*args, **kwargs)

        monkeypatch.setattr(container.signal_setup_service, "detach", fail_detach_once)
        failed_stop = client.post("/api/v1/processing/stop")
        assert failed_stop.status_code >= 400
        status = client.get("/api/v1/engine/status").json()["data"]
        assert status["acquisition_alive"] is True
        assert status["processor_alive"] is False
        assert container.engine_service.admission.requires_recovery("processing")
        assert container.engine_service._engine.processing_queue_dirty is False

        # Retrying Processing stop clears recovery now that the abandoned FIFO is flushed.
        monkeypatch.setattr(container.signal_setup_service, "detach", original_detach)
        retry_stop = client.post("/api/v1/processing/stop")
        assert retry_stop.status_code == 200, retry_stop.text
        assert not container.engine_service.admission.requires_recovery("processing")

        # Restart Processing while Acquisition remains running, then replay fresh trials.
        processing_rev = client.get("/api/v1/config/modules/processing").json()["data"]["revision"]
        restart = client.post("/api/v1/processing/start", json={
            "operation_id": str(uuid4()),
            "configuration": {**configs["processing"], "expected_revision": processing_rev},
        })
        assert restart.status_code == 200, restart.text
        assert restart.json()["data"]["status"] == "succeeded", restart.text

        if client.get("/api/v1/acquisition/status").json()["data"]["streaming"]:
            assert client.post("/api/v1/acquisition/stream/stop").status_code == 200
        connection = client.get("/api/v1/acquisition/status").json()["data"]["connection_id"]
        runtime = client.get("/api/v1/config/runtime").json()["data"]
        assert client.post("/api/v1/acquisition/stream/start", json={
            "expected_revision": runtime["applied"]["acquisition"],
            "generation": runtime["generation"],
            "connection_id": connection,
        }).status_code == 200
        fresh_trials = _wait_for_trials(client, minimum=len(initial_trials) + 1)
        assert len(fresh_trials) > len(initial_trials)

        assert client.post("/api/v1/acquisition/stream/stop").status_code == 200
        assert client.post("/api/v1/processing/stop").status_code == 200
        assert client.post("/api/v1/engine/stop").status_code == 200
        assert container.engine_service._engine.shutdown_complete is True
    assert app.state.shutdown_complete
