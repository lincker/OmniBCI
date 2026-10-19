"""
P4 tests — composition root wiring.

Covers the pure, I/O-free parts of omnibci.bootstrap:
  - assemble(): acquisition & stimulation share ONE engine instance; the
    session service is built on the given repository.
  - register_engine_listeners(): registers read-model + extra subscriptions via
    engine.subscribe, in order, and no-ops when the engine isn't ready.
  - _build_session_repository(): defaults to the persistent SQLite adapter.

build_container() itself is not unit-tested here because it performs real
engine startup; assemble() is the seam that lets us test wiring without it.
"""
from __future__ import annotations

from omnibci.bootstrap import (
    AppContainer,
    assemble,
    register_engine_listeners,
    _build_session_repository,
)
from omnibci.application.ports.engine_port import EnginePort
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
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


class _FakeSessionService:
    def on_session_started(self, event):  # pragma: no cover - identity only
        pass

    def on_trial_completed(self, event):  # pragma: no cover - identity only
        pass

    def on_session_ended(self, event):  # pragma: no cover - identity only
        pass

    def on_raw_recording_state(self, event):  # pragma: no cover - identity only
        pass


def _container(engine, session_service=None) -> AppContainer:
    from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager
    command_results = CommandResultBroker(engine)
    return AppContainer(
        engine_service=engine,
        acquisition_service=AcquisitionAppService(
            command_results,
        ),
        stimulation_service=StimulationAppService(engine),
        stimulation_process_manager=StimulationProcessManager(auto_spawn=False),
        session_repository=InMemorySessionRepository(),
        session_service=session_service or _FakeSessionService(),
        recording_service=None,  # not exercised by these wiring tests
        command_results=command_results,
    )


# ---------------------------------------------------------------------------
# assemble()
# ---------------------------------------------------------------------------

class TestAssemble:
    def test_acquisition_and_stimulation_share_one_engine(self):
        engine = FakeEngine()
        repo = InMemorySessionRepository()
        c = assemble(engine, repo)
        assert c.acquisition_service._command_executor is c.command_results
        assert c.stimulation_service._engine is engine
        assert c.stimulation_service._command_results is c.command_results
        assert c.engine_service is engine

    def test_session_service_uses_given_repository(self):
        repo = InMemorySessionRepository()
        c = assemble(FakeEngine(), repo)
        assert c.session_repository is repo
        assert c.session_service._repo is repo
        assert c.session_service._acquisition is c.acquisition_service
        assert c.session_service._signal_lock is c.acquisition_service.lifecycle_lock
        assert c.acquisition_service._validate_recording == c.session_service.validate_recording_before_stream


# ---------------------------------------------------------------------------
# register_engine_listeners()
# ---------------------------------------------------------------------------

class TestRegisterListeners:
    def test_registers_readmodel_then_extra_in_order(self):
        # Listener wiring is valid after graph construction and deliberately
        # does not require the runtime-ready state.
        engine = FakeEngine(initialized=True, ready=False)
        sess = _FakeSessionService()
        container = _container(engine, sess)

        def broadcaster_handler(event):
            pass

        extra = [(EventType.SessionStarted, broadcaster_handler)]
        count = register_engine_listeners(container, extra)

        assert count == 7
        registered_types = [t for t, _ in engine.subscriptions]
        assert registered_types == [
            EventType.CommandResult,
            EventType.SessionStarted,
            EventType.TrialCompleted,
            EventType.SessionEnded,
            EventType.AcquisitionState,
            EventType.RawRecordingState,
            EventType.SessionStarted,
        ]
        # read-model handlers come from the session / acquisition services
        assert engine.subscriptions[1][1] == sess.on_session_started
        assert engine.subscriptions[2][1] == sess.on_trial_completed
        assert engine.subscriptions[3][1] == sess.on_session_ended
        assert engine.subscriptions[4][1] == container.acquisition_service.on_acquisition_state
        assert engine.subscriptions[5][1] == sess.on_raw_recording_state
        # extra handler preserved
        assert engine.subscriptions[6][1] is broadcaster_handler

    def test_default_subscriptions_without_extra(self):
        engine = FakeEngine(initialized=True, ready=False)
        count = register_engine_listeners(_container(engine))
        assert count == 6

    def test_noop_when_engine_not_initialized(self):
        engine = FakeEngine(initialized=False)
        count = register_engine_listeners(_container(engine))
        assert count == 0
        assert engine.subscriptions == []


# ---------------------------------------------------------------------------
# _build_session_repository()
# ---------------------------------------------------------------------------

def test_build_session_repository_defaults_to_sqlite():
    from omnibci.infrastructure.repositories.sqlite_session_repository import SQLiteSessionRepository
    repo = _build_session_repository(None)
    assert isinstance(repo, SQLiteSessionRepository)
    repo.close()


def test_build_session_repository_in_memory_explicit():
    assert isinstance(_build_session_repository("in_memory"), InMemorySessionRepository)


def test_build_session_repository_unknown_kind_fails_fast():
    import pytest
    with pytest.raises(ValueError, match="unknown"):
        _build_session_repository("unknown")
