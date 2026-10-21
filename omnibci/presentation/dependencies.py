"""
FastAPI dependency providers.

All service singletons live on `app.state` (set during lifespan).
Routers depend only on these functions — never on concrete service classes.
"""
from fastapi import Request

from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.config_service import ConfigService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.results_service import ResultsService


def get_engine_service(request: Request) -> NeuroEngineService:
    return request.app.state.engine_service


def get_acquisition_service(request: Request) -> AcquisitionAppService:
    return request.app.state.acquisition_service


def get_stimulation_service(request: Request) -> StimulationAppService:
    return request.app.state.stimulation_service


def get_results_service(request: Request) -> ResultsService:
    return request.app.state.container.results_service


def get_config_service(request: Request) -> ConfigService:
    return request.app.state.config_service
