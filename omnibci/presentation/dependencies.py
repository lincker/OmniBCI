"""
FastAPI dependency providers.

All service singletons live on `app.state` (set during lifespan).
Routers depend only on these functions — never on concrete service classes.
"""
from fastapi import Request

from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.actuation_service import ActuationAppService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.plugin_management.market_service import PluginMarketService
from omnibci.application.plugin_management.package_service import PluginPackageService
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.results_service import ResultsService
from omnibci.presentation.errors import ApiError
from omnibci.presentation.schemas.common import ErrorCode


def _container_service(request: Request, attr: str, message: str):
    container = getattr(request.app.state, "container", None)
    service = getattr(container, attr, None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, message)
    return service


def get_engine_service(request: Request) -> NeuroEngineService:
    return request.app.state.engine_service


def get_acquisition_service(request: Request) -> AcquisitionAppService:
    return request.app.state.acquisition_service


def get_stimulation_service(request: Request) -> StimulationAppService:
    return request.app.state.stimulation_service


def get_actuation_service(request: Request) -> ActuationAppService:
    return request.app.state.actuation_service


def get_results_service(request: Request) -> ResultsService:
    return request.app.state.container.results_service


def get_configuration_service(request: Request) -> ConfigurationService:
    return _container_service(request, "configuration_service", "Configuration is unavailable")


def get_runtime_configuration(request: Request) -> RuntimeConfigurationService:
    return _container_service(request, "runtime_configuration", "Runtime configuration is unavailable")


def get_node_configuration_service(request: Request) -> NodeConfigurationService:
    return _container_service(request, "node_configuration", "Node configuration is unavailable")


def get_signal_setup_service(request: Request) -> SignalSetupService:
    return _container_service(request, "signal_setup_service", "Signal setup is unavailable")


def get_plugin_package_service(request: Request) -> PluginPackageService:
    service = getattr(request.app.state, "plugin_package_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Plugin package management is unavailable")
    return service


def get_plugin_market_service(request: Request) -> PluginMarketService:
    service = getattr(request.app.state, "plugin_market_service", None)
    if service is None:
        raise ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Plugin market is not configured")
    return service
