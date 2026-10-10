"""The signal editor validates drafts and keeps them separate from runtime."""
from contextlib import nullcontext
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.conf import OmniBCISettings
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.signal_setup import router


@pytest.fixture
def signal_client(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\nqueue_size = 100\n', encoding="utf-8")
    service = SignalSetupService(
        OmniBCISettings(path), SimpleNamespace(repository=SimpleNamespace(get_activation=lambda: None)),
        SimpleNamespace(admission=None, applied_snapshot=None, is_engine_ready=False,
                        configuration_change=lambda idle: nullcontext()),
        SimpleNamespace(cached_metadata=None), None,
        SimpleNamespace(names=lambda: ["standard_1020"], get=lambda name: {
            "name": name, "positions": {"O1": [0.0, -0.08, 0.04]},
            "coordinate_frame": "head", "unit": "m", "provider": "fixture 1",
        }),
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.container = SimpleNamespace(signal_setup_service=service)
    with TestClient(app) as client:
        yield client


def test_global_signal_writer_is_removed(signal_client):
    before = signal_client.get("/config/signal").json()["data"]
    assert signal_client.put("/config/signal", json={"setup": {"source_id": "fixture"}}).status_code == 405
    assert signal_client.get("/config/signal").json()["data"] == before


@pytest.mark.parametrize("patch", [
    {"schema_version": True}, {"schema_version": 1.0}, {"schema_version": 2},
    {"future_driver": {}}, {"assignments": [{"source_index": 0, "electrode": "O1"}]},
    {"reference": {"mode": "average", "channels": []}},
])
def test_malformed_drafts_are_rejected_without_writes(signal_client, patch):
    initial = signal_client.get("/config/signal").json()["data"]
    response = signal_client.post("/config/signal/preview", json={"processing_input": patch})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert signal_client.get("/config/signal").json()["data"]["revision"] == initial["revision"]


def test_processing_profile_preview_does_not_modify_configuration(signal_client):
    before = signal_client.get("/config/signal").json()["data"]
    response = signal_client.post("/config/signal/preview", json={
        "processing_input": {"outputs": ["O1"],
                             "reference": {"mode": "single", "channels": ["M1"]}},
        "metadata": {"source_id": "fixture", "sample_rate": 500,
                     "trigger_row_index": 2, "original_reference": "Cz", "channels": [
                         {"index": 0, "name": "O1", "signal_type": "eeg", "unit": "uV"},
                         {"index": 1, "name": "M1", "signal_type": "eeg", "unit": "uV"},
                     ]},
    })
    assert response.status_code == 200
    plan = response.json()["data"]["plan"]
    assert plan["coefficients"] == [[1e-6, -1e-6]]
    assert plan["source"]["output_row_count"] == 3
    assert signal_client.get("/config/signal").json()["data"] == before


@pytest.mark.parametrize("field", ["source_id", "assignments", "original_reference", "template", "manual_metadata"])
def test_processing_profile_preview_rejects_source_edits(signal_client, field):
    response = signal_client.post("/config/signal/preview", json={"processing_input": {field: "value"}})
    assert response.status_code == 422
