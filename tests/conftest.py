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
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


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
    engine.drain_abandoned_processing_queue = AsyncMock(return_value=True)
    engine.processing_queue_dirty = False
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


def _peer_host():
    from omnibci.application.ports.ipc_port import PeerHostPort
    host = MagicMock(spec=PeerHostPort)
    host.is_peer_connected.return_value = False
    host.is_peer_ready.return_value = True
    host.node_operation_id.side_effect = lambda role: host.ensure_peer_ready_sync.call_args.args[1]["CONFIG.operation_id"]
    host.cancel_unaccepted_peer.return_value = True
    host.is_current_peer_channel.side_effect = lambda role, channel: channel == "current"
    host.peer_connection_token.side_effect = lambda role: "socket" if host.is_peer_connected(role) else None
    return host


@pytest.fixture()
def stimulation_host():
    return _peer_host()


@pytest.fixture()
def actuation_host():
    return _peer_host()


@pytest.fixture()
def stimulation_snapshot():
    return {"CONFIG.module_revisions": {"stimulation": "a" * 64}, "STIMULATION.auto_spawn": False}


@pytest.fixture()
def stimulation_service(engine_service, stimulation_host, stimulation_snapshot):
    from omnibci.application.services.stimulation_service import StimulationAppService
    manager = MagicMock(is_alive=False, current_paradigm=None)
    manager.start.side_effect = lambda **kwargs: setattr(manager, "is_alive", True)
    manager.stop.side_effect = lambda: setattr(manager, "is_alive", False)
    return StimulationAppService(stimulation_host, manager, ImmediateCommandExecutor(stimulation_host),
                                 engine_service.admission, snapshot_provider=lambda: stimulation_snapshot)


@pytest.fixture()
def actuation_service(engine_service, actuation_host):
    from omnibci.application.services.actuation_service import ActuationAppService
    manager = MagicMock(is_alive=False, current_effector=None)
    manager.start.side_effect = lambda **kwargs: setattr(manager, "is_alive", True)
    manager.stop.side_effect = lambda: setattr(manager, "is_alive", False)
    from types import SimpleNamespace
    from omnibci.shared.events import EventType
    commands = MagicMock()
    owner = ActuationAppService(actuation_host, manager, commands, engine_service.admission)
    def control(event):
        snapshot = owner.applied_snapshot
        flags = ({"resumed": True, "paused": False} if event == EventType.ResumeActuation
                 else {"paused": True, "execution_complete": True, "outputs_settled": True})
        return SimpleNamespace(data={**flags, "generation": snapshot["CONFIG.generation"], "engine_revision": None,
                                     "module_revision": snapshot["CONFIG.module_revisions"]["actuation"]})
    commands.request.side_effect = control
    return owner


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
