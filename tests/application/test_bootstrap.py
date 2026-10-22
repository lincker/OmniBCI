"""
P4 tests — composition root wiring.

Covers the pure, I/O-free parts of omnibci.bootstrap:
  - assemble(): acquisition, stimulation and live results share ONE engine instance.
  - register_engine_listeners(): registers read-model + extra subscriptions via
    engine.subscribe, in order, and no-ops when the engine isn't ready.

build_container() itself is not unit-tested here because it performs real
engine startup; assemble() is the seam that lets us test wiring without it.
"""
from __future__ import annotations

from omnibci.bootstrap import (
    AppContainer,
    assemble,
    register_engine_listeners,
)
from omnibci.application.ports.engine_port import EnginePort
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.application.services.results_service import ResultsService
from omnibci.shared.events import Event, EventType


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeEngine(EnginePort):
    """Engine stub recording subscribe() calls; lifecycle flags toggleable."""

    def __init__(self, initialized: bool = True, ready: bool = False):
        self._initialized = initialized
        self._ready = ready
        self.subscriptions = []
        self.generation = "test-generation"

    def dispatch(self, event: Event) -> None:  # unused here
        pass

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def is_engine_ready(self) -> bool:
        return self._ready

    def subscribe(self, event_type, handler) -> None:
        self.subscriptions.append((event_type, handler))

    def read_acquisition_preview(self, capture_id, sequence_id=None) -> dict:
        raise NotImplementedError("Preview reads are not exercised by this wiring fake")


def _container(engine) -> AppContainer:
    from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager
    command_results = CommandResultBroker(engine)
    return AppContainer(
        engine_service=engine,
        acquisition_service=AcquisitionAppService(
            command_results,
        ),
        stimulation_service=StimulationAppService(engine),
        stimulation_process_manager=StimulationProcessManager(auto_spawn=False),
        results_service=ResultsService(engine),
        command_results=command_results,
    )


# ---------------------------------------------------------------------------
# assemble()
# ---------------------------------------------------------------------------

class TestAssemble:
    def test_acquisition_and_stimulation_share_one_engine(self):
        engine = FakeEngine()
        c = assemble(engine)
        assert c.acquisition_service._command_executor is c.command_results
        assert c.stimulation_service._engine is engine
        assert c.stimulation_service._command_results is c.command_results
        assert c.engine_service is engine
        assert c.acquisition_service._preview_provider == engine.read_acquisition_preview
        engine.generation = "next-generation"
        assert c.acquisition_service._generation_provider() == "next-generation"

    def test_results_follow_the_same_engine(self):
        engine = FakeEngine()
        c = assemble(engine)
        assert c.results_service.engine is engine


# ---------------------------------------------------------------------------
# register_engine_listeners()
# ---------------------------------------------------------------------------

class TestRegisterListeners:
    def test_registers_readmodel_then_extra_in_order(self):
        # Listener wiring is valid after graph construction and deliberately
        # does not require the runtime-ready state.
        engine = FakeEngine(initialized=True, ready=False)
        container = _container(engine)

        def broadcaster_handler(event):
            pass

        extra = [(EventType.TrialCompleted, broadcaster_handler)]
        count = register_engine_listeners(container, extra)

        assert count == 4
        registered_types = [t for t, _ in engine.subscriptions]
        assert registered_types == [
            EventType.CommandResult,
            EventType.TrialCompleted,
            EventType.AcquisitionState,
            EventType.TrialCompleted,
        ]
        assert engine.subscriptions[1][1] == container.results_service.on_trial_completed
        assert engine.subscriptions[2][1] == container.acquisition_service.on_acquisition_state
        assert engine.subscriptions[3][1] is broadcaster_handler

    def test_default_subscriptions_without_extra(self):
        engine = FakeEngine(initialized=True, ready=False)
        count = register_engine_listeners(_container(engine))
        assert count == 3

    def test_noop_when_engine_not_initialized(self):
        engine = FakeEngine(initialized=False)
        count = register_engine_listeners(_container(engine))
        assert count == 0
        assert engine.subscriptions == []
