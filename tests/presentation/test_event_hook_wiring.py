"""
Regression test — EventBus listeners are wired at app startup.

Before this fix, the lifespan stashed ``app.state._register_event_hooks`` but
nothing ever called it, so the WebSocket broadcaster and the session read-model
never received engine events.  The lifespan now calls
``register_engine_listeners`` directly during startup; this test pins that
behaviour so the dangling-hook regression cannot return.

The engine is replaced with a recording stub (ready=True) by patching
``build_container``, so we observe exactly which subscriptions the lifespan
registers — without starting a real NeuroEngine.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omnibci.application.ports.engine_port import EnginePort
from omnibci.bootstrap import AppContainer
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.session_service import SessionAppService
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager
from omnibci.shared.events import Event, EventType


class _RecordingEngine(EnginePort):
    """Records composition-time subscriptions before runtime startup."""

    def __init__(self) -> None:
        self.subscriptions: list[tuple] = []

    def dispatch(self, event: Event) -> None:  # unused here
        pass

    @property
    def is_initialized(self) -> bool:
        return True

    @property
    def is_engine_ready(self) -> bool:
        return False

    def subscribe(self, event_type, handler) -> None:
        self.subscriptions.append((event_type, handler))

    async def shutdown(self) -> None:  # called by lifespan on teardown
        pass


def _make_container() -> tuple[AppContainer, _RecordingEngine]:
    engine = _RecordingEngine()
    repo = InMemorySessionRepository()
    container = AppContainer(
        engine_service=engine,
        acquisition_service=AcquisitionAppService(engine),
        stimulation_service=StimulationAppService(engine),
        stimulation_process_manager=StimulationProcessManager(auto_spawn=False),
        session_repository=repo,
        session_service=SessionAppService(repository=repo),
        recording_service=None,  # not exercised by event-hook wiring tests
    )
    return container, engine


def test_lifespan_registers_engine_listeners(monkeypatch):
    from omnibci.presentation import main as main_mod

    container, engine = _make_container()

    async def _fake_build_container():
        return container

    monkeypatch.setattr(main_mod, "build_container", _fake_build_container)

    # Entering the TestClient context runs the lifespan startup.
    with TestClient(main_mod.create_app()):
        pass

    registered_types = [t for t, _ in engine.subscriptions]

    # 4 read-model (application) + 4 broadcaster (presentation) = 8
    assert len(engine.subscriptions) == 8
    assert EventType.AcquisitionState in registered_types
    # read-model sync
    assert registered_types.count(EventType.TrialCompleted) == 2   # session + broadcaster
    assert registered_types.count(EventType.SessionStarted) == 2   # session + broadcaster
    assert EventType.SessionEnded in registered_types
    # the session service's own handlers are present
    handlers = [h for _, h in engine.subscriptions]
    assert container.session_service.on_session_started in handlers
    assert container.session_service.on_trial_completed in handlers
    assert container.session_service.on_session_ended in handlers


def test_no_dangling_unused_hook_on_app_state(monkeypatch):
    """The old never-called ``_register_event_hooks`` stash must be gone."""
    from omnibci.presentation import main as main_mod

    container, _ = _make_container()

    async def _fake_build_container():
        return container

    monkeypatch.setattr(main_mod, "build_container", _fake_build_container)

    app = main_mod.create_app()
    with TestClient(app):
        assert not hasattr(app.state, "_register_event_hooks")
