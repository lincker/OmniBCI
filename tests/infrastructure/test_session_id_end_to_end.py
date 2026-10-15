"""End-to-end acceptance coverage for the unified session identity."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from omnibci.bootstrap import assemble
from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.neuroengine.builder import (
    NeuroEngineBuilder,
    NeuroEngineConfig,
)
from omnibci.infrastructure.repositories.session_repository import (
    InMemorySessionRepository,
)
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.signal_setup_service import SignalSetupService


_ROOT = Path(__file__).parents[2]
_CNT_FIXTURE = _ROOT / "tests" / "fixture" / "train.cnt"


def _assert_success(response, expected_status: int = 200):
    assert response.status_code == expected_status, response.text
    body = response.json()
    assert body["success"] is True
    return body.get("data")


@pytest.mark.integration
def test_file_reader_session_id_matches_websocket_and_rest(
    monkeypatch,
    tmp_path,
):
    """Exercise REST, real workers, file replay, EventBus, WebSocket, and REST readback."""
    assert _CNT_FIXTURE.is_file()

    monkeypatch.chdir(_ROOT)
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "matplotlib"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    from omnibci.presentation import main as main_mod

    engine_config = NeuroEngineConfig(
        queue_size=200,
        message_server_ip="127.0.0.1",
        message_server_port=0,
        baseline_duration=0.0,
        actuation_enabled=False,
    )

    from omnibci.application.services.plugin_config_initialization import SettingsPluginConfigInitializer
    from omnibci.application.services.plugin_profile_service import PluginProfileService
    from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
    from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
    seed = {**OmniBCISettings().saved_snapshot(), "ACQUISITION.signal_source": "file_reader",
            "ACQUISITION.data_file_path": str(_CNT_FIXTURE), "ACQUISITION.sample_rate": 1000,
            "ACQUISITION.channel_number": 10}
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: seed)
    accepted = SettingsPluginConfigInitializer(profiles).initialize(seed).snapshot
    snapshot = profiles.materialize(accepted)

    async def build_acceptance_container(**kwargs):
        engine_service = NeuroEngineService(
            engine_factory=lambda runtime: NeuroEngineBuilder(engine_config, settings_snapshot=runtime).build(),
            snapshot_provider=profiles.resolve_settings,
        )
        await engine_service.startup()
        container = assemble(
            engine_service,
            InMemorySessionRepository(),
            settings={**snapshot, "NEUROENGINE.command_timeout_s": 90.0},
            auto_spawn=False,
            admission=engine_service.admission,
        )
        signal = SignalSetupService(
            SimpleNamespace(read_saved=lambda: (seed, "saved")), profiles, engine_service,
            container.acquisition_service, container.command_results,
        )
        container.acquisition_service.set_signal_preparer(signal.prepare)
        container.session_service.set_signal_preparer(signal.prepare_session, container.acquisition_service.lifecycle_lock)
        return container

    monkeypatch.setattr(main_mod, "build_container", build_acceptance_container)
    app = main_mod.create_app()

    messages: list[dict] = []
    receiver_errors: list[BaseException] = []
    session_started = threading.Event()
    trial_received = threading.Event()
    session_ended = threading.Event()

    with TestClient(app, raise_server_exceptions=True) as client:
        with client.websocket_connect("/api/v1/ws/results") as websocket:
            def receive_events() -> None:
                try:
                    while not session_ended.is_set():
                        message = websocket.receive_json()
                        messages.append(message)
                        if message.get("event") == "SessionStarted":
                            session_started.set()
                        elif message.get("event") == "TrialCompleted":
                            trial_received.set()
                        elif message.get("event") == "SessionEnded":
                            session_ended.set()
                except BaseException as exc:
                    receiver_errors.append(exc)
                    session_started.set()
                    trial_received.set()
                    session_ended.set()

            receiver = threading.Thread(
                target=receive_events,
                daemon=True,
                name="session-id-e2e-ws-receiver",
            )
            receiver.start()

            engine_started = False
            try:
                _assert_success(client.post("/api/v1/engine/start"))
                engine_started = True
                _assert_success(client.post("/api/v1/acquisition/connect"))

                created = _assert_success(
                    client.post(
                        "/api/v1/sessions",
                        json={"paradigm": "classic40SSVEP"},
                    ),
                    expected_status=201,
                )
                session_id = created["session_id"]

                assert session_started.wait(30), "SessionStarted was not received"
                assert not receiver_errors
                started = next(
                    message
                    for message in messages
                    if message.get("event") == "SessionStarted"
                )
                assert started["session_id"] == session_id

                _assert_success(client.post("/api/v1/acquisition/stream/start"))

                assert trial_received.wait(90), "TrialCompleted was not received"
                assert not receiver_errors
                trial_message = next(
                    message
                    for message in messages
                    if message.get("event") == "TrialCompleted"
                )
                assert trial_message["session_id"] == session_id

                session = _assert_success(
                    client.get(f"/api/v1/sessions/{session_id}")
                )
                trials = _assert_success(
                    client.get(f"/api/v1/sessions/{session_id}/trials")
                )
                assert session["session_id"] == session_id
                assert session["signal_plan"]["processing_input"]["outputs"] == []
                assert "setup" not in session["signal_plan"]
                assert session["trial_count"] >= 1
                assert trials
                assert trials[0]["trial_number"] == trial_message["trial_number"]
                assert trials[0]["event_id"] == trial_message["event_id"]

                _assert_success(client.post("/api/v1/engine/stop"))
                engine_started = False

                assert session_ended.wait(10), "SessionEnded was not received"
                assert not receiver_errors
                ended = next(
                    message
                    for message in messages
                    if message.get("event") == "SessionEnded"
                )
                assert ended["session_id"] == session_id

                final_session = _assert_success(
                    client.get(f"/api/v1/sessions/{session_id}")
                )
                final_trials = _assert_success(
                    client.get(f"/api/v1/sessions/{session_id}/trials")
                )
                assert final_session["state"] == "ended"
                assert final_session["trial_count"] == len(final_trials)
                assert ended["total_trials"] == len(final_trials)

                receiver.join(timeout=5)
                assert not receiver.is_alive()
            finally:
                if engine_started and app.state.engine_service.is_engine_ready:
                    client.post("/api/v1/engine/stop")
