"""
Composition Root — the single place where concrete implementations are bound
to their abstractions and application services are wired together.

Why a dedicated module
----------------------
Before P4, service construction and EventBus listener registration lived inside
``presentation/main.py``'s FastAPI lifespan.  That mixed two concerns: *defining
the HTTP app* and *assembling the object graph*.  Pulling assembly out into this
module means:

  - any entrypoint (HTTP server, CLI, a test harness, a future worker process)
    can build the exact same wired graph by calling ``build_container()``;
  - the wiring rules live in one auditable place, driven by ``settings.toml``;
  - ``main.py`` shrinks to "build the container, expose it over HTTP".

Dependency direction
---------------------
This is the outermost layer — it is allowed to import application,
infrastructure and configuration.  Nothing else imports *it* except concrete
entrypoints (e.g. ``presentation/main.py``).
"""

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Tuple

from omnibci.shared.events import EventType
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.recording_service import RecordingService
from omnibci.domain.repositories import SessionRepository
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager

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


# ---------------------------------------------------------------------------
# Concrete-implementation selection (config-driven)
# ---------------------------------------------------------------------------

def _build_session_repository() -> SessionRepository:
    """
    Pick the SessionRepository implementation from settings.

    settings key: PERSISTENCE.session_repository  (default: "in_memory")

    Adding a SQLite/Postgres adapter later means registering it here — no
    application-layer code changes, since everything depends on the
    SessionRepository port.
    """
    kind = "in_memory"
    try:
        # Lazy import so this module stays importable without a settings file
        # (e.g. in pure unit tests of the wiring helpers below).
        from omnibci.conf import settings
        kind = settings.get("PERSISTENCE.session_repository")
    except Exception:
        kind = "in_memory"

    if kind in (None, "in_memory"):
        return InMemorySessionRepository()

    raise ValueError(f"Unknown session_repository implementation: {kind!r}")


# ---------------------------------------------------------------------------
# Pure wiring (no I/O) — unit-testable without starting an engine
# ---------------------------------------------------------------------------

def assemble(
    engine_service: NeuroEngineService,
    repository: SessionRepository,
) -> AppContainer:
    """
    Wire application services around an (already-created) engine service and
    repository.  Acquisition & Stimulation share the *same* engine instance via
    the EnginePort abstraction.
    """
    acquisition_service = AcquisitionAppService(engine_service)
    session_service = SessionAppService(repository=repository,
                                        engine_port=engine_service)

    # Read auto_spawn from config; default True for single-machine convenience.
    try:
        from omnibci.conf import settings as _settings
        auto_spawn = bool(_settings.get("STIMULATION.auto_spawn", True))
    except Exception:
        auto_spawn = True
    process_manager = StimulationProcessManager(auto_spawn=auto_spawn)

    return AppContainer(
        engine_service=engine_service,
        acquisition_service=acquisition_service,
        stimulation_service=StimulationAppService(engine_service, process_manager),
        stimulation_process_manager=process_manager,
        session_repository=repository,
        # engine_port lets create/end push the session id into the worker
        # (StartSession/EndSession) — see docs/session-id-unification.md.
        session_service=session_service,
        # Orchestrates session + acquisition for the "record a run" use case.
        recording_service=RecordingService(session_service, acquisition_service),
    )


# ---------------------------------------------------------------------------
# Full construction (performs engine startup)
# ---------------------------------------------------------------------------

async def build_container() -> AppContainer:
    """
    Build the complete object graph.

    Constructs the NeuroEngineService and runs its (non-blocking) startup so the
    engine object — and its EventBus — exist before listeners are registered.
    """
    engine_service = NeuroEngineService()
    await engine_service.startup()
    return assemble(engine_service, _build_session_repository())


# ---------------------------------------------------------------------------
# EventBus listener registration
# ---------------------------------------------------------------------------

def register_engine_listeners(
    container: AppContainer,
    extra_subscriptions: Iterable[Subscription] = (),
) -> int:
    """
    Subscribe read-model and any extra (e.g. presentation) listeners to the
    engine's EventBus.  Must be called *after* the engine process is running.

    Application-level subscriptions (read-model sync) are owned here; the caller
    supplies presentation-level ones (e.g. WebSocket broadcaster) via
    ``extra_subscriptions`` so this module need not import the presentation layer.

    Returns the number of subscriptions registered (0 if the engine isn't ready).
    """
    engine = container.engine_service
    if not engine.is_engine_ready:
        logger.warning("register_engine_listeners: engine not ready — skipped")
        return 0

    subscriptions: list[Subscription] = [
        (EventType.SessionStarted, container.session_service.on_session_started),
        (EventType.TrialCompleted, container.session_service.on_trial_completed),
        (EventType.SessionEnded, container.session_service.on_session_ended),
        *extra_subscriptions,
    ]
    for event_type, handler in subscriptions:
        engine.subscribe(event_type, handler)

    logger.info("Engine listeners registered: %d", len(subscriptions))
    return len(subscriptions)
