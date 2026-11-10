"""The processing editor previews channel mappings without changing runtime."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.conf import OmniBCISettings
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.routers.signal_setup import router


@pytest.fixture
def signal_client(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\nqueue_size = 100\n', encoding="utf-8")
    service = SignalSetupService(
        OmniBCISettings(path), SimpleNamespace(repository=SimpleNamespace(get_activation=lambda: None)),
        SimpleNamespace(admission=None, applied_snapshot=None, is_engine_ready=False),
        SimpleNamespace(cached_descriptor_for=lambda binding: None), None,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.container = SimpleNamespace(signal_setup_service=service)
    with TestClient(app) as client:
        yield client


def test_global_signal_writer_and_montage_read_contract_are_removed(signal_client):
    before = signal_client.get("/config/signal").json()["data"]
    assert not {"setup", "legacy_setup", "accepted_setup", "active_setup", "source_metadata"} & before.keys()
    assert signal_client.put("/config/signal", json={"setup": {"source_id": "fixture"}}).status_code == 405
    assert signal_client.get("/config/signal").json()["data"] == before


@pytest.mark.parametrize("patch", [
    {"schema_version": True}, {"schema_version": 2.0}, {"schema_version": 1},
    {"future_driver": {}}, {"assignments": []}, {"excluded_channels": []},
    {"reference": {"mode": "original"}}, {"template": "standard_1020"},
    {"outputs": ["Oz"]}, {"outputs": [{"name": "Oz", "source": "CH1"}]},
    {"outputs": [{"name": "Oz", "source_channel_id": 1}]},
    {"outputs": [{"name": "Oz", "source_channel_id": ""}]},
    {"outputs": [{"name": "Oz", "source_channel_id": "channel-0", "signal_type": "trigger"}]},
    {"outputs": [{"name": "Oz", "source_channel_id": "channel-0"},
                 {"name": "O1", "source_channel_id": "channel-0"}]},
])
def test_malformed_drafts_are_rejected_without_writes(signal_client, patch):
    initial = signal_client.get("/config/signal").json()["data"]
    response = signal_client.post("/config/signal/preview", json={"processing_input": patch})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert signal_client.get("/config/signal").json()["data"]["revision"] == initial["revision"]


def connected_descriptor(signal_client, signal_type="unknown"):
    service = signal_client.app.state.container.signal_setup_service
    service.engine.is_engine_ready = True
    service.engine.applied_snapshot = {"ACQUISITION.signal_source": "fixture"}
    descriptor = SignalInfo("fixture", 500, (
        ChannelDescriptor(0, "CH1", signal_type=signal_type, unit="uV"),
        ChannelDescriptor(1, "CH1", unit="uV"),
    ), 2, connection_id="current-connection")
    service.acquisition.cached_descriptor_for = lambda binding: descriptor.to_dict()
    return descriptor


@pytest.mark.parametrize("source", [{"source_channel_id": "channel-1"}, {"source_channel_index": 1}])
def test_preview_uses_channel_identity_and_preserves_acquisition_properties(signal_client, source):
    descriptor = connected_descriptor(signal_client)
    before = signal_client.get("/config/signal").json()["data"]
    channel_input = {"schema_version": 2, "outputs": [
        {"name": "Oz", **source, "signal_type": "eeg"}]}
    response = signal_client.post("/config/signal/preview", json={
        "processing_input": channel_input, "expected_source_revision": before["source_revision"],
    })
    assert response.status_code == 200
    plan = response.json()["data"]["plan"]
    assert plan["source_indices"] == [1]
    assert plan["output_channels"] == [{"channel_id": "channel-1", "index": 0, "name": "Oz",
                                        "signal_type": "eeg", "unit": "uV"}]
    assert plan["processing_input"] == channel_input
    assert plan["source"] == descriptor.to_dict()
    assert not {"setup", "coefficients", "warnings"} & plan.keys()
    assert signal_client.get("/config/signal").json()["data"] == before


def test_declared_type_cannot_override_known_acquisition_type(signal_client):
    connected_descriptor(signal_client, signal_type="eog")
    response = signal_client.post("/config/signal/preview", json={"processing_input": {"outputs": [
        {"name": "Oz", "source_channel_id": "channel-0", "signal_type": "eeg"}]}})
    assert response.status_code == 422
    assert "conflicts" in response.json()["message"]


def test_preview_rejects_caller_supplied_acquisition_properties(signal_client):
    response = signal_client.post("/config/signal/preview", json={
        "processing_input": {}, "acquisition_descriptor": {},
    })
    assert response.status_code == 422


def test_preview_rejects_stale_acquisition_context(signal_client):
    response = signal_client.post("/config/signal/preview", json={
        "processing_input": {}, "expected_source_revision": "old-source",
    })
    assert response.status_code == 409


@pytest.mark.parametrize("field", ["source_id", "assignments", "original_reference", "template", "manual_metadata"])
def test_processing_preview_rejects_retired_source_edits(signal_client, field):
    response = signal_client.post("/config/signal/preview", json={"processing_input": {field: "value"}})
    assert response.status_code == 422


def test_preview_passes_exact_edited_decoder_selection(signal_client):
    service = signal_client.app.state.container.signal_setup_service
    received = []
    service.preview_input = lambda value, **kwargs: (received.append(kwargs) or {
        "plan": {}, "warnings": [], "source_revision": "current"})
    selection = {"package_id": "author.decoder", "contribution_id": "decode", "version": "1.0.0"}
    response = signal_client.post("/config/signal/preview", json={
        "processing_input": {"outputs": []}, "decoder_selection": selection,
    })
    assert response.status_code == 200
    assert received[0]["decoder_selection"] == selection
    response = signal_client.post("/config/signal/preview", json={
        "processing_input": {}, "decoder_selection": {**selection, "unknown": True},
    })
    assert response.status_code == 422
