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
def acquisition_service(engine_service):
    from omnibci.application.services.acquisition_service import AcquisitionAppService
    return AcquisitionAppService(engine_service)


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
