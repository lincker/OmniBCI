"""Composition root for application services and infrastructure adapters."""

import logging
import json
from dataclasses import dataclass
from typing import Callable, Iterable, Tuple

from omnibci.shared.events import EventType
from omnibci.application.activation_settings import backend_startup_settings
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.recording_service import RecordingService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.application.services.config_service import ConfigService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.plugin_config_initialization import SettingsPluginConfigInitializer
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.application.services.plugin_installation_service import PluginInstallationService
from omnibci.application.services.plugin_package_service import PluginPackageService
from omnibci.application.services.plugin_market_service import PluginMarketService
from omnibci.application.plugin_packages import package_references_from_settings
from omnibci.application.plugin_profiles import ProfileBinding, ProfileRevisionConflict, PluginProfileNotFoundError
from omnibci.domain.repositories import SessionRepository
from omnibci.infrastructure.backend_logging import BackendLogging
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings
from omnibci.infrastructure.plugins.plugin_catalog import user_plugins_dir
from omnibci.infrastructure.plugins.package_lifecycle_store import ManagedPluginPackageStore
from omnibci.infrastructure.plugins.market_adapter import configured_market_service
from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager
from omnibci.infrastructure.actuation.process_manager import ActuationProcessManager

logger = logging.getLogger(__name__)

# A single EventBus subscription: (event type, handler callable).
Subscription = Tuple[EventType, Callable]


def load_host_settings() -> dict:
    """Capture saved inputs once for all backend startup consumers."""
    from omnibci.conf import settings

    return settings.saved_snapshot()


def create_backend_logging(host_settings: dict) -> BackendLogging:
    """Configure backend logging from the same snapshot as backend assembly."""
    return BackendLogging(host_settings)


def _effective_backend_startup_settings(snapshot: dict) -> dict:
    values = backend_startup_settings(snapshot)
    directory = user_plugins_dir(host_settings=snapshot)
    values["PLUGINS.user_dir"] = str(directory.resolve()) if directory is not None else None
    return values


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
    config_service: "ConfigService | None" = None
    plugin_profile_service: "PluginProfileService | None" = None
    runtime_configuration: "RuntimeConfigurationService | None" = None
    node_configuration: "NodeConfigurationService | None" = None
    plugin_installation_service: "PluginInstallationService | None" = None
    plugin_package_service: "PluginPackageService | None" = None
    plugin_market_service: "PluginMarketService | None" = None
    signal_setup_service: object | None = None


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
    acquisition_settings=None,
    auto_spawn: bool = True,
    actuation_process_manager: "ActuationProcessManager | None" = None,
    stimulation_process_manager: "StimulationProcessManager | None" = None,
    admission=None,
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
        admission=admission,
    )
    acquisition_service = AcquisitionAppService(
        command_results,
        settings=acquisition_settings if acquisition_settings is not None else settings,
        admission=admission,
    )
    session_service = SessionAppService(
        repository=repository,
        engine_port=engine_service,
        command_results=command_results,
        admission=admission,
    )

    process_manager = stimulation_process_manager or StimulationProcessManager(auto_spawn=auto_spawn)

    return AppContainer(
        engine_service=engine_service,
        acquisition_service=acquisition_service,
        stimulation_service=StimulationAppService(
            engine_service,
            process_manager,
            command_results=command_results,
            admission=admission,
        ),
        stimulation_process_manager=process_manager,
        session_repository=repository,
        # engine_port lets create/end push the session id into the worker
        # (StartSession/EndSession) — see docs/session-id-unification.md.
        session_service=session_service,
        # Orchestrates session + acquisition for the "record a run" use case.
        recording_service=RecordingService(session_service, acquisition_service,
                                            admission=admission),
        actuation_process_manager=actuation_process_manager,
        command_results=command_results,
    )


# ---------------------------------------------------------------------------
# Full construction (performs engine startup)
# ---------------------------------------------------------------------------

async def build_container(*, host_settings: dict | None = None,
                          allow_process_termination: bool = True) -> AppContainer:
    """Wire services; resolve configuration and build workers on engine start."""
    from omnibci.conf import settings

    host_settings = load_host_settings() if host_settings is None else dict(host_settings)
    repository = _build_session_repository(
        host_settings.get("PERSISTENCE.session_repository", "in_memory"))
    auto_spawn = bool(host_settings.get("STIMULATION.auto_spawn", True))
    # Actuation subprocess management: disabled subsystem → auto_spawn=False,
    # making every lifecycle hook a no-op (multi-machine runs the script).
    actuation_auto_spawn = (
        bool(host_settings.get("ACTUATION.enabled", False))
        and bool(host_settings.get("ACTUATION.auto_spawn", True))
    )
    actuation_manager = ActuationProcessManager(
        auto_spawn=actuation_auto_spawn, allow_process_termination=allow_process_termination,
    )
    stimulation_manager = StimulationProcessManager(
        auto_spawn=auto_spawn, allow_process_termination=allow_process_termination,
    )

    # Processes are single-use, so every restart requires a fresh engine graph.
    def startup_snapshot():
        return accepted_runtime_settings(profiles=plugin_profiles)

    engine_service = NeuroEngineService(
        engine_factory=lambda snapshot: NeuroEngineBuilder(
            settings_snapshot=snapshot, allow_process_termination=allow_process_termination,
        ).build(),
        snapshot_provider=startup_snapshot,
        actuation_process_manager=actuation_manager,
        stimulation_process_manager=stimulation_manager,
    )
    config_service = ConfigService(
        settings,
        change_guard=engine_service.configuration_change,
    )
    plugin_profiles = PluginProfileService(
        TomlPluginProfileRepository(), CatalogConfigurationAdapter(),
        settings_source=settings.saved_snapshot,
        change_guard=engine_service.configuration_change,
        startup_settings_resolver=_effective_backend_startup_settings,
        loaded_startup_settings=host_settings,
    )
    try:
        initializer = SettingsPluginConfigInitializer(plugin_profiles)
        if initializer.existing() is None:
            result = initializer.initialize(host_settings)
            for warning in result.warnings:
                logger.warning("Plugin configuration initialization: %s", warning)
    except (ValueError, KeyError, RuntimeError, OSError):
        # Keep configuration management available so invalid saved data can be
        # repaired. Starting workers still requires a valid accepted snapshot.
        logger.exception("Plugin configuration initialization requires attention")
    await engine_service.startup()
    container = assemble(engine_service, repository,
                         settings=host_settings, acquisition_settings=_RuntimeSettings(engine_service, plugin_profiles),
                         auto_spawn=auto_spawn,
                         actuation_process_manager=actuation_manager,
                         stimulation_process_manager=stimulation_manager,
                         admission=engine_service.admission)
    container.config_service = config_service
    container.plugin_profile_service = plugin_profiles
    container.acquisition_service.set_metadata_recorder(
        lambda profile_id: _acquisition_montage_binding(engine_service, plugin_profiles, profile_id),
        plugin_profiles.record_acquisition_montage,
    )
    from omnibci.application.services.signal_setup_service import SignalSetupService
    from omnibci.infrastructure.signal_layouts import SignalLayoutProvider
    container.signal_setup_service = SignalSetupService(
        settings, plugin_profiles, engine_service, container.acquisition_service,
        container.command_results, SignalLayoutProvider(),
    )
    container.acquisition_service.set_signal_preparer(container.signal_setup_service.prepare)
    container.session_service.set_signal_preparer(
        container.signal_setup_service.prepare_session, container.acquisition_service.lifecycle_lock,
    )
    container.node_configuration = NodeConfigurationService(engine_service, plugin_profiles)
    container.runtime_configuration = RuntimeConfigurationService(
        plugin_profiles, engine_service,
        executor=RuntimeApplyCoordinator(engine_service, plugin_profiles, container.acquisition_service,
                                          container.session_service, container.stimulation_service,
                                          container.command_results,
                                          signal_preparer=container.signal_setup_service.prepare),
    )
    packages = PluginPackageService(
        ManagedPluginPackageStore(), plugin_profiles,
        runtime_references=lambda: tuple(
            reference
            for snapshot in engine_service.runtime_package_snapshots
            for reference in package_references_from_settings(snapshot)
        ),
        admission_guard=engine_service.admission.transition,
    )
    container.plugin_installation_service = packages
    container.plugin_package_service = packages
    container.plugin_market_service = configured_market_service(
        packages.store, admission_guard=engine_service.admission.transition,
    )
    return container


def _acquisition_montage_binding(engine, profiles, profile_id=None):
    """Resolve the running source, never a saved selection awaiting Apply."""
    snapshot = engine.applied_snapshot or {}
    value = next((binding for binding in snapshot.get("PLUGINS.bindings", ())
                  if binding.get("slot") == "acquisition.source"), None)
    binding = ProfileBinding.from_dict(value) if value is not None else None
    if profile_id is None:
        return binding
    if binding is None or binding.profile_id != profile_id:
        raise ProfileRevisionConflict("Connect the applied acquisition profile before reading its montage")
    try:
        saved = profiles.repository.get_profile(profile_id)
    except PluginProfileNotFoundError as exc:
        raise ProfileRevisionConflict("Acquisition profile no longer exists") from exc
    if (saved.kind != binding.kind or saved.selection != binding.selection
            or saved.configuration_version != binding.configuration_version
            or json.dumps(saved.parameters, sort_keys=True) != json.dumps(binding.parameters, sort_keys=True)):
        raise ProfileRevisionConflict("Acquisition profile differs from the running source; Apply it before reading montage")
    return binding


class _RuntimeSettings:
    """Status metadata follows the running snapshot, including after saved edits."""
    def __init__(self, engine, profiles):
        self.engine = engine
        self.profiles = profiles

    def get(self, key, default=None):
        snapshot = self.engine.applied_snapshot
        if snapshot is None or not self.engine.is_engine_ready:
            try:
                accepted = self.profiles.repository.get_activation()
                if accepted is None:
                    return default
                snapshot = self.profiles.materialize(accepted)
            except (ValueError, KeyError, RuntimeError, OSError):
                # Optional status metadata must not block configuration repair.
                return default
        return snapshot.get(key, default)


# ---------------------------------------------------------------------------
# EventBus listener registration
# ---------------------------------------------------------------------------

def register_engine_listeners(
    container: AppContainer,
    extra_subscriptions: Iterable[Subscription] = (),
) -> int:
    """
    Subscribe read-model and any extra (e.g. presentation) listeners to the
    engine's EventBus. The service must be initialised; subscriptions are
    replayed when the graph is built at the actual engine start.

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
