"""
OmniBCI REST + WebSocket API server.

Start with:
    uvicorn omnibci.presentation.main:app --host 0.0.0.0 --port 8000 --reload

Endpoints:
    GET  /healthz                          Health check
    POST /api/v1/engine/start              Start NeuroEngine
    POST /api/v1/engine/stop               Stop NeuroEngine
    GET  /api/v1/engine/status             Engine status

    POST /api/v1/acquisition/connect       Connect data source
    POST /api/v1/acquisition/disconnect    Disconnect data source
    POST /api/v1/acquisition/stream/start  Start data stream
    POST /api/v1/acquisition/stream/stop   Stop data stream
    GET  /api/v1/acquisition/status        Acquisition status

    GET  /api/v1/stimulation/paradigms     List paradigms (legacy)
    GET  /api/v1/plugins/{category}        List plugins by category
    POST /api/v1/stimulation/start         Start stimulation
    POST /api/v1/stimulation/stop          Stop stimulation
    POST /api/v1/stimulation/trial/start   Trigger a trial

    WS   /api/v1/ws/results                Real-time classification results
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from omnibci.presentation.routers import engine, acquisition, stimulation, session, recording, config, plugins
from omnibci.presentation.routers import module_config, plugin_config, runtime_config
from omnibci.presentation.websockets.results import results_broadcaster, results_ws_endpoint
from omnibci.bootstrap import build_container, register_engine_listeners
from omnibci.presentation.errors import register_exception_handlers
from omnibci.presentation.schemas.common import ErrorResponse
from omnibci.shared.events import EventType

logger = logging.getLogger(__name__)

# Documents the unified error envelope (ErrorResponse) on every endpoint so it
# appears in the OpenAPI spec and the generated frontend types.
ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    404: {"model": ErrorResponse, "description": "Not found"},
    409: {"model": ErrorResponse, "description": "Conflict"},
    422: {"model": ErrorResponse, "description": "Validation error"},
    500: {"model": ErrorResponse, "description": "Internal error"},
    503: {"model": ErrorResponse, "description": "Service unavailable"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup / shutdown lifecycle.

    Assembly of the object graph lives in the composition root
    (``omnibci.bootstrap``); this lifespan only:
      1. builds the container and exposes its services on app.state;
      2. gives the ResultsBroadcaster the running asyncio loop;
      3. stores a hook that registers EventBus listeners (engine read-model +
         WebSocket broadcaster) once the engine process is actually running.
    """
    container = await build_container()

    app.state.container = container
    app.state.engine_service = container.engine_service
    app.state.acquisition_service = container.acquisition_service
    app.state.stimulation_service = container.stimulation_service
    app.state.stimulation_process_manager = container.stimulation_process_manager
    app.state.session_repository = container.session_repository
    app.state.session_service = container.session_service
    app.state.recording_service = container.recording_service
    app.state.config_service = container.config_service

    results_broadcaster.set_loop(asyncio.get_running_loop())
    # Heartbeat: reaps dead WS connections during idle periods and lets the
    # browser detect a silently dead feed (it cannot see protocol-level pings).
    results_broadcaster.start_heartbeat()

    # Record EventBus listeners now; the graph is built at engine start.
    # Presentation
    # subscriptions are injected here so bootstrap stays free of any presentation
    # import.
    broadcaster_subscriptions = [
        (EventType.TrialCompleted, results_broadcaster.on_trial_completed),
        (EventType.SessionStarted, results_broadcaster.on_session_started),
        (EventType.SessionEnded, results_broadcaster.on_session_ended),
        # Relay stimulation acks (from the stim process via the socket) to clients.
        (EventType.StimulationAck, results_broadcaster.on_stimulation_ack),
    ]
    registered = register_engine_listeners(container, broadcaster_subscriptions)

    logger.info("OmniBCI API server started (engine listeners registered: %d)", registered)
    yield
    results_broadcaster.stop_heartbeat()
    if container.runtime_configuration is not None:
        await container.runtime_configuration.wait_for_idle()
    container.stimulation_process_manager.stop()
    await container.engine_service.shutdown()
    logger.info("OmniBCI API server stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="OmniBCI API",
        version="1.0.0",
        description="REST + WebSocket API for the OmniBCI BCI platform",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Unified error envelope for every failure (ApiError / HTTPException /
    # validation / unexpected) — see omnibci.presentation.errors.
    register_exception_handlers(app)

    prefix = "/api/v1"
    app.include_router(engine.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(acquisition.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(stimulation.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(session.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(recording.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(config.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(module_config.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(plugin_config.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(runtime_config.router, prefix=prefix, responses=ERROR_RESPONSES)
    app.include_router(plugins.router, prefix=prefix, responses=ERROR_RESPONSES)

    @app.websocket("/api/v1/ws/results")
    async def ws_results(websocket: WebSocket):
        await results_ws_endpoint(websocket)

    @app.get("/healthz", tags=["Health"])
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
