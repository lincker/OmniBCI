"""Composition root for application services and infrastructure adapters."""

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Tuple

from omnibci.shared.events import EventType
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.recording_service import RecordingService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.domain.repositories import SessionRepository
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager
from omnibci.infrastructure.actuation.process_manager import ActuationProcessManager

logger = logging.getLogger(__name__)

# A single EventBus subscription: (event type, handler callable).
Subscription = Tuple[EventType, Callable]


@dataclass
class AppContainer:
    """The fully-wired object graph shared across the process."""
    engine_service: NeuroEngineService
    acquisition_service: AcquisitionAppService
    stimulation_service: StimulationAppService
    stimulation_process_manager: StimulationProcessManager
    session_repository: SessionRepository
    session_service: SessionAppService
    recording_service: RecordingService
    # Optional so existing test constructions stay valid; the composition
    # root always supplies one (auto_spawn=False when ACTUATION disabled).
    actuation_process_manager: "ActuationProcessManager | None" = None
    # Shared correlation rendezvous for commands that must report actual worker
    # execution before REST returns success.
    command_results: "CommandResultBroker | None" = None


# ---------------------------------------------------------------------------
# Concrete-implementation selection (config-driven)
# ---------------------------------------------------------------------------

def _build_session_repository(kind: "str | None") -> SessionRepository:
    """Build the configured repository or fail on an unknown implementation."""
    if kind in (None, "in_memory"):
        return InMemorySessionRepository()

    raise ValueError(f"Unknown session_repository implementation: {kind!r}")


# ---------------------------------------------------------------------------
# Pure wiring (no I/O) — unit-testable without starting an engine
# ---------------------------------------------------------------------------

def assemble(
    engine_service: NeuroEngineService,
    repository: SessionRepository,
    *,
    settings=None,
    auto_spawn: bool = True,
    actuation_process_manager: "ActuationProcessManager | None" = None,
) -> AppContainer:
    """Wire services around an existing engine and repository without I/O."""
    command_timeout_s = (
        float(settings.get("NEUROENGINE.command_timeout_s", 10.0))
        if settings is not None
        else 10.0
    )
    command_results = CommandResultBroker(
        engine_service,
        timeout_s=command_timeout_s,
    )
    acquisition_service = AcquisitionAppService(
        command_results,
        settings=settings,
    )
    session_service = SessionAppService(
        repository=repository,
        engine_port=engine_service,
        command_results=command_results,
    )

    process_manager = StimulationProcessManager(auto_spawn=auto_spawn)

    return AppContainer(
        engine_service=engine_service,
        acquisition_service=acquisition_service,
        stimulation_service=StimulationAppService(
            engine_service,
            process_manager,
            command_results=command_results,
        ),
        stimulation_process_manager=process_manager,
        session_repository=repository,
        # engine_port lets create/end push the session id into the worker
        # (StartSession/EndSession) — see docs/session-id-unification.md.
        session_service=session_service,
        # Orchestrates session + acquisition for the "record a run" use case.
        recording_service=RecordingService(session_service, acquisition_service),
        actuation_process_manager=actuation_process_manager,
        command_results=command_results,
    )


# ---------------------------------------------------------------------------
# Full construction (performs engine startup)
# ---------------------------------------------------------------------------

async def build_container() -> AppContainer:
    """Build and start the complete object graph from process-wide settings."""
    from omnibci.conf import settings

    repository = _build_session_repository(
        settings.get("PERSISTENCE.session_repository", "in_memory"))
    auto_spawn = bool(settings.get("STIMULATION.auto_spawn", True))
    # Actuation subprocess management: disabled subsystem → auto_spawn=False,
    # making every lifecycle hook a no-op (multi-machine runs the script).
    actuation_auto_spawn = (
        bool(settings.get("ACTUATION.enabled", False))
        and bool(settings.get("ACTUATION.auto_spawn", True))
    )
    actuation_manager = ActuationProcessManager(auto_spawn=actuation_auto_spawn)

    # Processes are single-use, so every restart requires a fresh engine graph.
    engine_service = NeuroEngineService(
        engine_factory=lambda: NeuroEngineBuilder().build(),
        actuation_process_manager=actuation_manager,
    )
    await engine_service.startup()
    return assemble(engine_service, repository,
                    settings=settings, auto_spawn=auto_spawn,
                    actuation_process_manager=actuation_manager)


# ---------------------------------------------------------------------------
# EventBus listener registration
# ---------------------------------------------------------------------------

def register_engine_listeners(
    container: AppContainer,
    extra_subscriptions: Iterable[Subscription] = (),
) -> int:
    """
    Subscribe read-model and any extra (e.g. presentation) listeners to the
    engine's EventBus.  The graph must already be initialised, but the engine
    process need not be running: listener wiring is a composition-time action.

    Application-level subscriptions (read-model sync) are owned here; the caller
    supplies presentation-level ones (e.g. WebSocket broadcaster) via
    ``extra_subscriptions`` so this module need not import the presentation layer.

    Returns the number of subscriptions registered (0 if no graph exists yet).
    """
    engine = container.engine_service
    if not engine.is_initialized:
        logger.warning("register_engine_listeners: engine not initialised — skipped")
        return 0

    subscriptions: list[Subscription] = [
        *(
            ((EventType.CommandResult, container.command_results.on_result),)
            if container.command_results is not None
            else ()
        ),
        (EventType.SessionStarted, container.session_service.on_session_started),
        (EventType.TrialCompleted, container.session_service.on_trial_completed),
        (EventType.SessionEnded, container.session_service.on_session_ended),
        (EventType.AcquisitionState, container.acquisition_service.on_acquisition_state),
        *extra_subscriptions,
    ]
    for event_type, handler in subscriptions:
        engine.subscribe(event_type, handler)

    logger.info("Engine listeners registered: %d", len(subscriptions))
    return len(subscriptions)
