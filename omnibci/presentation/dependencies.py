"""
FastAPI dependency providers.

All service singletons live on `app.state` (set during lifespan).
Routers depend only on these functions — never on concrete service classes.
"""
from fastapi import Request

from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.results_service import ResultsService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ErrorCode


def get_engine_service(request: Request) -> NeuroEngineService:
    return request.app.state.engine_service


def get_acquisition_service(request: Request) -> AcquisitionAppService:
    return request.app.state.acquisition_service


def get_stimulation_service(request: Request) -> StimulationAppService:
    return request.app.state.stimulation_service


def get_results_service(request: Request) -> ResultsService:
    return request.app.state.container.results_service


def get_configuration_service(request: Request) -> ConfigurationService:
    container = getattr(request.app.state, "container", None)
    service = getattr(container, "configuration_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Configuration is unavailable")
    return service
