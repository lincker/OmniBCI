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


# ---------------------------------------------------------------------------
# Concrete-implementation selection (config-driven)
# ---------------------------------------------------------------------------

def _build_session_repository(kind: "str | None") -> SessionRepository:
    """
    Pick the SessionRepository implementation for *kind*
    (settings key: PERSISTENCE.session_repository; None/"in_memory" → 内存实现).

    P2-9：改为接收值而非自行读全局 settings —— 配置读取集中在
    build_container()。未知实现名 fail-fast（ValueError），不再静默降级。

    Adding a SQLite/Postgres adapter later means registering it here — no
    application-layer code changes, since everything depends on the
    SessionRepository port.
    """
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
    """
    Wire application services around an (already-created) engine service and
    repository.  Acquisition & Stimulation share the *same* engine instance via
    the EnginePort abstraction.

    P2-9：本函数保持纯净（零 I/O、零全局读取）——settings 与 auto_spawn
    由调用方（build_container）读取一次后传入；单测可传 None/默认值。
    """
    acquisition_service = AcquisitionAppService(engine_service, settings=settings)
    session_service = SessionAppService(repository=repository,
                                        engine_port=engine_service)

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
        actuation_process_manager=actuation_process_manager,
    )


# ---------------------------------------------------------------------------
# Full construction (performs engine startup)
# ---------------------------------------------------------------------------

async def build_container() -> AppContainer:
    """
    Build the complete object graph.

    Constructs the NeuroEngineService and runs its (non-blocking) startup so the
    engine object — and its EventBus — exist before listeners are registered.

    P2-9：这里是 API 进程组合期**唯一**的 settings 读取点。配置文件缺失/
    损坏在 conf import 时即刻炸出（fail-fast），可选键经 get(key, 默认值)
    显式给默认——组合路径上不再有 except Exception 静默降级。
    """
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

    # 每次调用构建全新引擎图（restart 语义）；重启时重读 settings，
    # 使 engine-restart 级配置变更生效 —— 与迁移前 NeuroEngineBuilder()
    # 在 service 内部的行为一致。
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
        (EventType.AcquisitionState, container.acquisition_service.on_acquisition_state),
        *extra_subscriptions,
    ]
    for event_type, handler in subscriptions:
        engine.subscribe(event_type, handler)

    logger.info("Engine listeners registered: %d", len(subscriptions))
    return len(subscriptions)
