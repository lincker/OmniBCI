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
def isolated_recording_directory(monkeypatch, tmp_path):
    """Keep raw recordings in temporary storage inherited by engine workers."""
    monkeypatch.setenv("OMNIBCI_RECORDING_DIR", str(tmp_path / "recordings"))


@pytest.fixture(autouse=True)
def isolated_backend_log_directory(monkeypatch, tmp_path):
    """Keep API lifespan logs out of the real platform user log directory."""
    from omnibci.infrastructure import backend_logging

    monkeypatch.setattr(backend_logging, "user_log_path", lambda *args, **kwargs: tmp_path / "logs")


class ImmediateCommandExecutor:
    """Explicit test double: dispatches once and confirms immediately."""

    def __init__(self, engine):
        self.engine = engine
        self.requests = []

    def request(self, event_type, payload=None, *, timeout_s=None):
        from omnibci.application.events import CommandResult
        from omnibci.shared.events import Event

        self.requests.append((event_type, payload))
        self.engine.dispatch(Event(event_type, payload))
        return CommandResult(
            command_id="test-command",
            command=event_type.name,
            ok=True,
        )

# ---------------------------------------------------------------------------
# Mock NeuroEngine so importing application services never triggers hardware
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_engine():
    """A MagicMock that looks like NeuroEngine."""
    engine = MagicMock()
    engine.event_bus = MagicMock()
    engine.event_bus.put_event = MagicMock()
    engine.event_bus.register_event_listener = MagicMock()
    engine.acquisition_worker = MagicMock()
    engine.acquisition_worker.is_alive.return_value = True
    engine.signal_processing_worker = MagicMock()
    engine.signal_processing_worker.is_alive.return_value = True
    engine.should_stop = False
    return engine


# ---------------------------------------------------------------------------
# Application service fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine_service(mock_engine):
    from omnibci.application.services.engine_service import NeuroEngineService
    svc = NeuroEngineService(engine_factory=lambda: mock_engine)
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
def session_repository():
    from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
    return InMemorySessionRepository()


@pytest.fixture()
def session_service(session_repository):
    from omnibci.application.services.session_service import SessionAppService
    return SessionAppService(repository=session_repository)


# ---------------------------------------------------------------------------
# FastAPI TestClient
# Overrides app.state services with mock-backed instances so no real engine
# is ever started during HTTP tests.
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(engine_service, acquisition_service, stimulation_service, session_service, session_repository):
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
            app.state.engine_service = engine_service
            app.state.acquisition_service = acquisition_service
            app.state.stimulation_service = stimulation_service
            app.state.session_repository = session_repository
            app.state.session_service = session_service
            yield client
