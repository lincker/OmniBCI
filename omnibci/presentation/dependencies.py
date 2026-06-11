"""
FastAPI dependency providers.

All service singletons live on `app.state` (set during lifespan).
Routers depend only on these functions — never on concrete service classes.
"""
from fastapi import Request

from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.recording_service import RecordingService
from omnibci.domain.repositories import SessionRepository


def get_engine_service(request: Request) -> NeuroEngineService:
    return request.app.state.engine_service


def get_acquisition_service(request: Request) -> AcquisitionAppService:
    return request.app.state.acquisition_service


def get_stimulation_service(request: Request) -> StimulationAppService:
    return request.app.state.stimulation_service


def get_session_service(request: Request) -> SessionAppService:
    return request.app.state.session_service


def get_recording_service(request: Request) -> RecordingService:
    return request.app.state.recording_service


def get_session_repository(request: Request) -> SessionRepository:
    return request.app.state.session_repository
