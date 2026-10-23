"""
Regression test — EventBus listeners are wired at app startup.

Before this fix, the lifespan stashed ``app.state._register_event_hooks`` but
nothing ever called it, so the WebSocket broadcaster and the results read-model
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
from omnibci.application.services.results_service import ResultsService
from omnibci.application.services.command_result_service import CommandResultBroker
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
    command_results = CommandResultBroker(engine)
    container = AppContainer(
        engine_service=engine,
        acquisition_service=AcquisitionAppService(
            command_results,
        ),
        stimulation_service=StimulationAppService(engine),
        stimulation_process_manager=StimulationProcessManager(auto_spawn=False),
        results_service=ResultsService(engine),
        command_results=command_results,
    )
    return container, engine


def test_lifespan_registers_engine_listeners(monkeypatch):
    from omnibci.presentation import main as main_mod

    container, engine = _make_container()

    async def _fake_build_container(**kwargs):
        return container

    monkeypatch.setattr(main_mod, "build_container", _fake_build_container)

    # Entering the TestClient context runs the lifespan startup.
    with TestClient(main_mod.create_app()):
        pass

    registered_types = [t for t, _ in engine.subscriptions]

    assert len(engine.subscriptions) == 6
    assert EventType.CommandResult in registered_types
    assert EventType.AcquisitionState in registered_types
    assert registered_types.count(EventType.TrialCompleted) == 2
    assert registered_types.count(EventType.EpochProcessed) == 1
    assert EventType.StimulationAck in registered_types
    handlers = [handler for _, handler in engine.subscriptions]
    assert container.results_service.on_trial_completed in handlers
    assert container.results_service.on_epoch_processed in handlers
    assert container.acquisition_service.on_acquisition_state in handlers
