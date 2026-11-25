"""
Shared pytest fixtures for api & application tests.

Design principles:
- NeuroEngine and its subprocesses are fully mocked; tests run without hardware.
- FastAPI app is created fresh per test session via TestClient.
- Each service fixture is stateless and independent.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated_module_config_directory(monkeypatch, tmp_path):
    """Never read or write real user profiles in tests, including API lifespan."""
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(tmp_path / "module-config"))


@pytest.fixture(autouse=True)
def isolated_plugin_directory(monkeypatch, tmp_path):
    """Each server test owns its store and configuration reference scope."""
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))


@pytest.fixture(autouse=True)
def isolated_server_log_directory(monkeypatch, tmp_path):
    """Keep API lifespan logs out of the real platform user log directory."""
    from omnibci.infrastructure import server_logging

    monkeypatch.setattr(server_logging, "user_log_path", lambda *args, **kwargs: tmp_path / "logs")


class ImmediateCommandExecutor:
    """Explicit test double: dispatches once and confirms immediately."""

    def __init__(self, engine):
        self.engine = engine
        self.requests = []

    def request(self, event_type, payload=None, *, timeout_s=None):
        from omnibci.application.events import CommandResult, StreamDrainReceipt
        from omnibci.shared.events import Event

        self.requests.append((event_type, payload))
        self.engine.dispatch(Event(event_type, payload))
        return CommandResult(
            command_id="test-command",
            command=event_type.name,
            ok=True,
            data=StreamDrainReceipt(None, None, -1, 0, False).to_dict()
                 if event_type.name == "StopAndDrain" else None,
        )

# ---------------------------------------------------------------------------
# Mock NeuroEngine so importing application services never triggers hardware
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_engine():
    """A MagicMock that looks like NeuroEngine."""
    engine = MagicMock()
    engine.dispatch = MagicMock()
    engine.subscribe = MagicMock()
    engine.is_peer_connected.return_value = False
    engine.is_current_peer_channel.return_value = False
    engine.acquisition_worker = MagicMock()
    engine.acquisition_worker.is_alive.return_value = False
    engine.signal_processing_worker = MagicMock()
    engine.signal_processing_worker.is_alive.return_value = False
    workers = {"acquisition": engine.acquisition_worker, "processing": engine.signal_processing_worker}
    engine.is_module_running.side_effect = lambda module: workers[module].is_alive()

    async def start_module(module, snapshot):
        workers[module].is_alive.return_value = True

    async def stop_module(module):
        workers[module].is_alive.return_value = False

    engine.start_module = AsyncMock(side_effect=start_module)
    engine.stop_module = AsyncMock(side_effect=stop_module)
    engine.set_actuation_active = AsyncMock()
    engine.ensure_output_ready = AsyncMock()
    engine.shutdown_complete = True
    return engine


# ---------------------------------------------------------------------------
# Application service fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine_service(mock_engine):
    from omnibci.application.services.engine_service import NeuroEngineService
    svc = NeuroEngineService(engine_factory=lambda snapshot: mock_engine)
    svc._engine = mock_engine
    svc._running = False
    return svc


@pytest.fixture()
def command_executor(engine_service):
    return ImmediateCommandExecutor(engine_service)


@pytest.fixture()
def acquisition_service(engine_service, command_executor):
    from omnibci.application.services.acquisition_service import AcquisitionAppService
    return AcquisitionAppService(command_executor)


@pytest.fixture()
def stimulation_service(engine_service):
    from omnibci.application.services.stimulation_service import StimulationAppService
    return StimulationAppService(engine_service)


@pytest.fixture()
def actuation_service(engine_service, command_executor):
    from omnibci.application.services.actuation_service import ActuationAppService
    return ActuationAppService(engine_service, commands=command_executor)


# ---------------------------------------------------------------------------
# FastAPI TestClient
# Overrides app.state services with mock-backed instances so no real engine
# is ever started during HTTP tests.
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(engine_service, acquisition_service, stimulation_service, actuation_service):
    from omnibci.presentation.main import create_app

    # Patch lifespan so it does NOT start real processes
    with patch(
        "omnibci.application.services.engine_service.NeuroEngineService.startup",
        new_callable=AsyncMock,
    ), patch(
        "omnibci.application.services.engine_service.NeuroEngineService.shutdown",
        new_callable=AsyncMock,
    ):
        app = create_app()

        # Inject mock services before first request
        with TestClient(app, raise_server_exceptions=True) as client:
            engine_service._stimulation = stimulation_service
            engine_service._actuation = actuation_service
            app.state.engine_service = engine_service
            app.state.acquisition_service = acquisition_service
            app.state.stimulation_service = stimulation_service
            app.state.actuation_service = actuation_service
            yield client
