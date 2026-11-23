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
        engine.subscribe(EventType.ActuationAck, acknowledged)
        for module, configuration in configurations.items():
            path = "/acquisition/connect" if module == "acquisition" else f"/engine/modules/{module}/start"
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
