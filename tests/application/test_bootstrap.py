"""
P4 tests — composition root wiring.

Covers the pure, I/O-free parts of omnibci.bootstrap:
  - assemble(): acquisition, stimulation and live results share ONE engine instance.
  - register_engine_listeners(): registers read-model + extra subscriptions via
    engine.subscribe, in order, and no-ops when the engine isn't ready.

build_container() itself is not unit-tested here because it performs real
engine startup; assemble() is the seam that lets us test wiring without it.
"""
from __future__ import annotations

import pytest

from omnibci.bootstrap import (
    AppContainer,
    assemble,
    register_engine_listeners,
)
from omnibci.application.ports.engine_port import EnginePort
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.stimulation_service import StimulationAppService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.application.services.results_service import ResultsService
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
        self.generation = "test-generation"

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

    def on_peer_delivery_fault(self, event):
        pass

    def read_acquisition_preview(self, capture_id, sequence_id=None) -> dict:
        raise NotImplementedError("Preview reads are not exercised by this wiring fake")


def _container(engine) -> AppContainer:
    from omnibci.infrastructure.stimulation.process_manager import StimulationProcessManager
    command_results = CommandResultBroker(engine)
    return AppContainer(
        engine_service=engine,
        acquisition_service=AcquisitionAppService(
            command_results,
        ),
        stimulation_service=StimulationAppService(engine),
        stimulation_process_manager=StimulationProcessManager(),
        results_service=ResultsService(lambda: engine.generation),
        command_results=command_results,
    )


# ---------------------------------------------------------------------------
# assemble()
# ---------------------------------------------------------------------------

class TestAssemble:
    def test_acquisition_and_stimulation_share_one_engine(self):
        engine = FakeEngine()
        c = assemble(engine)
        assert c.acquisition_service._command_executor is c.command_results
        assert c.stimulation_service._engine is engine
        assert c.stimulation_service._command_results is c.command_results
        assert c.engine_service is engine
        assert c.acquisition_service._preview_provider == engine.read_acquisition_preview
        engine.generation = "next-generation"
        assert c.acquisition_service._generation_provider() == "next-generation"

    def test_results_follow_the_same_engine(self):
        engine = FakeEngine()
        c = assemble(engine)
        assert c.results_service.snapshot()["generation"] == engine.generation
        engine.generation = "next-generation"
        assert c.results_service.snapshot()["generation"] == "next-generation"


# ---------------------------------------------------------------------------
# register_engine_listeners()
# ---------------------------------------------------------------------------

class TestRegisterListeners:
    def test_processing_input_fault_updates_its_read_model(self):
        from unittest.mock import Mock

        engine = FakeEngine(initialized=True)
        container = _container(engine)
        container.signal_setup_service = Mock()
        register_engine_listeners(container)
        assert (EventType.ProcessingInputFault, container.signal_setup_service.on_input_fault) in engine.subscriptions

    def test_registers_readmodel_then_extra_in_order(self):
        # Listener wiring is valid after graph construction and deliberately
        # does not require the runtime-ready state.
        engine = FakeEngine(initialized=True, ready=False)
        container = _container(engine)

        def broadcaster_handler(event):
            pass

        extra = [(EventType.TrialCompleted, broadcaster_handler)]
        count = register_engine_listeners(container, extra)

        assert count == 6
        registered_types = [t for t, _ in engine.subscriptions]
        assert registered_types == [
            EventType.CommandResult,
            EventType.TrialCompleted,
            EventType.EpochProcessed,
            EventType.AcquisitionState,
            EventType.PeerDeliveryFault,
            EventType.TrialCompleted,
        ]
        assert engine.subscriptions[1][1] == container.results_service.on_trial_completed
        assert engine.subscriptions[2][1] == container.results_service.on_epoch_processed
        assert engine.subscriptions[3][1] == container.acquisition_service.on_acquisition_state
        assert engine.subscriptions[4][1] == engine.on_peer_delivery_fault
        assert engine.subscriptions[5][1] is broadcaster_handler

    def test_default_subscriptions_without_extra(self):
        engine = FakeEngine(initialized=True, ready=False)
        count = register_engine_listeners(_container(engine))
        assert count == 5

    def test_noop_when_engine_not_initialized(self):
        engine = FakeEngine(initialized=False)
        count = register_engine_listeners(_container(engine))
        assert count == 0
        assert engine.subscriptions == []


@pytest.fixture
def bootstrap_settings(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from omnibci import bootstrap
    from omnibci.application.configuration.definitions import SERVER_STARTUP_DEFAULTS
    from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository

    settings = TomlConfigurationRepository()
    settings.save_server(SERVER_STARTUP_DEFAULTS, expected_revision=settings.read_server()[1])
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    builder = Mock(side_effect=AssertionError("Bootstrap must not construct a worker graph"))
    monkeypatch.setattr(bootstrap, "NeuroEngineBuilder", builder)
    yield settings
    builder.assert_not_called()


def _toml_files(root):
    return {path: path.read_bytes() for path in root.rglob("*.toml")}


def _configure(container):
    from tests.plugin_configuration_helpers import accept_profiles

    return accept_profiles(container.configuration_service, (
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": "recordings/custom.cnt", "sample_rate": 256,
            "channel_count": 5, "file_reader_chunk_size": 17,
        }),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40FBCCA", {
            "window_duration": 4.25, "offset_time": 0.15,
        }),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
    ))


@pytest.mark.asyncio
async def test_saved_edits_do_not_change_explicitly_selected_startup():
    from omnibci.bootstrap import build_container

    container = await build_container()
    try:
        profiles = container.configuration_service
        accepted = _configure(container)
        before = container.engine_service._module_snapshot_provider("acquisition")
        source = next(binding for binding in accepted.bindings() if binding.kind == "acquisition")
        profile = profiles.repository.get_profile(source.profile_id)
        parameters = {**profile.parameters, "sample_rate": 250}
        profiles.update(profile.profile_id, name="Saved edit", parameters=parameters,
                        expected_revision=profile.revision)

        assert container.engine_service._module_snapshot_provider("acquisition") == before
        assert before["CONFIG.module_revisions"]["acquisition"] == accepted.modules["acquisition"].revision
        assert [binding["slot"] for binding in before["PLUGINS.bindings"]] == ["acquisition.source"]
        assert container.engine_service._snapshot_provider()["PLUGINS.bindings"] == []
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_fresh_installation_does_not_select_plugins():
    from omnibci.bootstrap import build_container

    container = await build_container()
    try:
        profiles = container.configuration_service
        assert all(value is None for value in profiles.selected_configuration().revisions().values())
        assert profiles.repository.list_profiles() == []
        assert container.acquisition_service._device_metadata() == (None, None, None)
        snapshot = container.engine_service._snapshot_provider()
        assert snapshot["PLUGINS.bindings"] == []
        assert profiles.repository.get_engine() is not None
        assert all(module is None for module in profiles.selected_configuration().modules.values())
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


def test_status_reads_applied_values_while_engine_running():
    from unittest.mock import Mock
    from omnibci.application.configuration.models import ActivationSet
    from omnibci.bootstrap import _RuntimeSettings

    engine = Mock(is_engine_ready=True, applied_snapshot={"ACQUISITION.sample_rate": 250})
    profiles = Mock()
    profiles.selected_configuration.return_value = ActivationSet({}, None)
    profiles.materialize.return_value = {"ACQUISITION.sample_rate": 1000}
    view = _RuntimeSettings(engine, profiles)
    assert view.get("ACQUISITION.sample_rate") == 250
    profiles.materialize.assert_not_called()
    engine.is_engine_ready = False
    assert view.get("ACQUISITION.sample_rate") == 1000


@pytest.mark.asyncio
async def test_stream_prepares_only_processing_and_keeps_receipt(monkeypatch):
    from unittest.mock import Mock
    from omnibci.application.services.signal_setup_service import SignalSetupService
    from omnibci.bootstrap import build_container

    calls = []
    plan = {"schema_version": 2}
    monkeypatch.setattr(SignalSetupService, "prepare", lambda self: calls.append("processing") or plan)
    container = await build_container()
    try:
        container.actuation_process_manager.start = Mock()

        assert container.acquisition_service._prepare_signal() == plan
        assert calls == ["processing"]
        container.actuation_process_manager.start.assert_not_called()
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_metadata_binding_uses_the_running_source_without_reading_saved_profiles():
    from unittest.mock import Mock
    from omnibci.bootstrap import _acquisition_binding, build_container

    container = await build_container()
    try:
        profiles = container.configuration_service
        accepted = _configure(container)
        binding = next(item for item in accepted.bindings() if item.slot == "acquisition.source")
        engine = Mock(applied_snapshot=profiles.materialize(accepted))
        assert _acquisition_binding(engine) == binding.to_dict()
        profile = profiles.repository.get_profile(binding.profile_id)
        profiles.update(profile.profile_id, name=profile.name,
                        parameters={**profile.parameters, "sample_rate": 250}, expected_revision=profile.revision)
        assert _acquisition_binding(engine) == binding.to_dict()
        engine.applied_snapshot = None
        assert _acquisition_binding(engine) is None
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_server_restart_changes_command_timeout_without_blocking_engine_review(bootstrap_settings):
    from omnibci.bootstrap import build_container
    from omnibci.infrastructure.config_paths import config_root

    first = await build_container()
    before = _configure(first)
    bootstrap_settings.save_server({"NEUROENGINE.command_timeout_s": 15.0},
                                   expected_revision=bootstrap_settings.read_server()[1])
    original_files = _toml_files(config_root())

    plan = first.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                            settings=before.engine.settings)
    assert plan.target_revisions["engine"] == before.engine.revision
    assert not plan.errors
    assert first.command_results._timeout_s == 10.0
    assert _toml_files(config_root()) == original_files

    await first.engine_service.shutdown()
    first.package_owner.close()
    restarted = await build_container()
    try:
        assert restarted.command_results._timeout_s == 15.0
        plan = restarted.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                                    settings=before.engine.settings)
        assert plan.target_revisions["engine"] == before.engine.revision
        assert plan.changed_settings == ()
        assert not plan.errors
        assert _toml_files(config_root()) == original_files
    finally:
        await restarted.engine_service.shutdown()
        restarted.package_owner.close()


@pytest.mark.asyncio
async def test_plugin_directory_edits_do_not_block_engine_review(bootstrap_settings, monkeypatch, tmp_path):
    from omnibci.bootstrap import build_container
    from omnibci.infrastructure.config_paths import config_root

    container = await build_container()
    try:
        before = _configure(container)
        bootstrap_settings.save_server({"PLUGINS.user_dir": str(tmp_path / "masked-by-environment")},
                                       expected_revision=bootstrap_settings.read_server()[1])
        original_files = _toml_files(config_root())

        plan = container.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                                    settings=before.engine.settings)

        assert plan.target_revisions["engine"] == before.engine.revision
        assert not plan.errors
        monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "changed-effective-path"))
        plan = container.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                                    settings=before.engine.settings)
        assert not plan.errors
        assert _toml_files(config_root()) == original_files
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_existing_activation_keeps_its_revision_and_pinned_inputs(bootstrap_settings):
    from omnibci.bootstrap import build_container

    first = await build_container()
    profiles = first.configuration_service
    current = _configure(first)
    historical = current
    source = next(binding for binding in historical.bindings() if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    profiles.update(profile.profile_id, name="Saved source edit",
                    parameters={**profile.parameters, "sample_rate": 1024},
                    expected_revision=profile.revision)
    bootstrap_settings.save_server({"LOGGING.level": "DEBUG"},
                                   expected_revision=bootstrap_settings.read_server()[1])
    original_files = _toml_files(profiles.repository.root)
    original_settings = (bootstrap_settings.root / "settings.toml").read_bytes()

    await first.engine_service.shutdown()
    first.package_owner.close()
    restarted = await build_container()
    try:
        assert restarted.configuration_service.selected_configuration() == historical
        runtime = restarted.engine_service._module_snapshot_provider("acquisition")
        assert runtime["CONFIG.module_revisions"]["acquisition"] == historical.modules["acquisition"].revision
        assert runtime["ACQUISITION.sample_rate"] == 256
        assert runtime["ACQUISITION.channel_count"] == 5
        assert not {"ALGORITHM.window_duration", "ssvep.duration",
                    "ACQUISITION.parallel_port_address"} & runtime.keys()
        processing_runtime = restarted.engine_service._module_snapshot_provider("processing")
        decoder = next(binding for binding in processing_runtime["PLUGINS.bindings"]
                       if binding["kind"] == "decoder")
        selected_decoder = next(binding for binding in historical.bindings() if binding.kind == "decoder")
        assert decoder["selection"] == selected_decoder.selection.to_dict()
        assert decoder["parameters"] == {"window_duration": 4.25, "offset_time": 0.15}
        assert _toml_files(profiles.repository.root) == original_files
        assert (bootstrap_settings.root / "settings.toml").read_bytes() == original_settings
    finally:
        await restarted.engine_service.shutdown()
        restarted.package_owner.close()


@pytest.mark.asyncio
async def test_broken_activation_keeps_management_available_without_replacement(bootstrap_settings):
    from omnibci.bootstrap import build_container

    first = await build_container()
    repository = first.configuration_service.repository
    path = repository.root / "activation" / "acquisition.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('format_version = "unterminated', encoding="utf-8")
    original_files = _toml_files(repository.root)

    await first.engine_service.shutdown()
    first.package_owner.close()
    container = await build_container()
    try:
        assert container.configuration_service.read_server()[0][0]
        assert container.acquisition_service._device_metadata() == (None, None, None)
        foundation = container.engine_service._snapshot_provider()
        assert foundation["PLUGINS.bindings"] == []
        with pytest.raises(RuntimeError, match="configuration is invalid"):
            container.engine_service._module_snapshot_provider("acquisition")
        assert path.read_bytes() == original_files[path]
        assert repository.get_engine() is not None
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_saved_edit_during_bootstrap_does_not_change_loaded_startup_baseline(bootstrap_settings):
    from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS
    from omnibci.bootstrap import build_container

    loaded, revision = bootstrap_settings.read_server()
    bootstrap_settings.save_server({"NEUROENGINE.command_timeout_s": 20.0}, expected_revision=revision)
    container = await build_container(host_settings=loaded)
    try:
        service = container.configuration_service

        assert container.command_results._timeout_s == 10.0
        candidate = service.propose_engine(ENGINE_SETTING_DEFAULTS)
        assert candidate.settings == ENGINE_SETTING_DEFAULTS
        assert "NEUROENGINE.command_timeout_s" not in candidate.settings
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_bootstrap_shares_pinned_plugin_selection_between_status_and_engine(monkeypatch, tmp_path):
    from unittest.mock import patch
    from omnibci import bootstrap
    from omnibci.application.commands.engine_commands import StartEngineCommand
    from omnibci.application.configuration.models import EngineSettings
    from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
    from tests.plugin_configuration_helpers import builtin_selection

    repository = TomlConfigurationRepository()
    repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 32}), expected_revision=None)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    with patch.object(bootstrap, "NeuroEngineBuilder") as builder:
        container = await bootstrap.build_container()
        try:
            builder.assert_not_called()
            profiles = container.configuration_service
            selected = profiles.create("acquisition", builtin_selection(
                "acquisition", "neuroscan_scan45_reader"), "Lab", {
                    "DATA_PROVIDER_IP": "192.168.0.3", "DATA_PROVIDER_PORT": 4000,
                    "sample_rate": 500, "channel_count": 10,
                })
            bindings = [{"slot": "acquisition.source", "profile_id": selected.profile_id}]
            for slot, kind, alias in (
                ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
                ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
                ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
            ):
                selected = profiles.create(kind, builtin_selection(kind, alias), alias, {})
                bindings.append({"slot": slot, "profile_id": selected.profile_id})
            profiles.repository.save_engine(profiles.propose_engine({"NEUROENGINE.queue_size": 32}),
                                            expected_revision=profiles.repository.get_engine().revision)
            for module in ("acquisition", "processing", "stimulation"):
                profiles.repository.save_module(profiles.propose(module, [
                    binding for binding in bindings if binding["slot"].startswith(f"{module}.")
                ], settings=profiles.default_module_settings(module)), expected_revision=None)
            assert container.acquisition_service._device_metadata() == ("OmniBCI.neuroscan_scan45_reader/default@0.1.0", None, None)
            with patch("omnibci.application.services.engine_service.Thread"):
                await container.engine_service.handle_start_engine(StartEngineCommand())
            snapshot = builder.call_args.kwargs["settings_snapshot"]
            assert snapshot["PLUGINS.bindings"] == []
            source_snapshot = container.engine_service._module_snapshot_provider("acquisition")
            assert source_snapshot["ACQUISITION.sample_rate"] == 500
            assert source_snapshot["ACQUISITION.DATA_PROVIDER_PORT"] == 4000
        finally:
            container.package_owner.close()


@pytest.mark.asyncio
async def test_bootstrap_rejects_second_controller_before_runtime_construction():
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from omnibci.bootstrap import build_container

    first = await build_container()
    try:
        with pytest.raises(PluginPackageConflictError, match="owner"):
            await build_container()
    finally:
        await first.engine_service.shutdown()
        first.package_owner.close()


def test_shutdown_prepare_and_resume_keep_ownership_until_lifespan_exits(tmp_path):
    from fastapi.testclient import TestClient
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from omnibci.infrastructure.config_paths import config_root
    from omnibci.presentation.main import create_app
    from tests.infrastructure.test_plugin_store_ownership import claim

    app = create_app()
    with TestClient(app) as client:
        assert client.portal.call(app.state.shutdown_service.prepare) == []
        with pytest.raises(PluginPackageConflictError, match="owner"):
            claim(tmp_path / "plugins", configuration_root=config_root())
        app.state.shutdown_service.resume()
        with pytest.raises(PluginPackageConflictError, match="owner"):
            claim(tmp_path / "plugins", configuration_root=config_root())
    next_owner = claim(tmp_path / "plugins", configuration_root=config_root())
    next_owner.close()


def test_failed_final_shutdown_does_not_admit_another_owner(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from omnibci.infrastructure.config_paths import config_root
    from omnibci.presentation.main import create_app
    from tests.infrastructure.test_plugin_store_ownership import claim

    app = create_app()
    with pytest.raises(RuntimeError, match="resources still held"):
        with TestClient(app):
            monkeypatch.setattr(app.state.engine_service, "shutdown",
                                AsyncMock(side_effect=RuntimeError("resources still held")))
    with pytest.raises(PluginPackageConflictError, match="unconfirmed shutdown"):
        claim(tmp_path / "plugins", configuration_root=config_root())


def test_presentation_startup_failure_still_settles_the_acquired_owner(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from omnibci.infrastructure.config_paths import config_root
    from omnibci.presentation import main
    from omnibci.presentation.main import create_app
    from tests.infrastructure.test_plugin_store_ownership import claim

    monkeypatch.setattr(main, "register_engine_listeners",
                        Mock(side_effect=RuntimeError("listener registration failed")))
    with pytest.raises(RuntimeError, match="listener registration failed"):
        with TestClient(create_app()):
            pytest.fail("Startup should fail")
    owner = claim(tmp_path / "plugins", configuration_root=config_root())
    owner.close()


@pytest.fixture
def isolated_composition(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(tmp_path / "configuration"))
    for name in ("OMNIBCI_MARKET_URL", "OMNIBCI_MARKET_REPOSITORY_ID", "OMNIBCI_MARKET_TRUSTED_KEYS", "OMNIBCI_MARKET_CACHE_DIR"):
        monkeypatch.delenv(name, raising=False)
    builder = Mock(side_effect=AssertionError("Package APIs must not construct an engine graph"))
    monkeypatch.setattr("omnibci.bootstrap.NeuroEngineBuilder.build", builder)
    yield builder
    builder.assert_not_called()


def _configure_market(monkeypatch, tmp_path):
    import base64
    import json
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from tests.infrastructure.test_market_repository import _URL

    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("OMNIBCI_MARKET_URL", _URL)
    monkeypatch.setenv("OMNIBCI_MARKET_REPOSITORY_ID", "example.plugins")
    monkeypatch.setenv("OMNIBCI_MARKET_TRUSTED_KEYS", json.dumps({
        "test-key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
    }))
    monkeypatch.setenv("OMNIBCI_MARKET_CACHE_DIR", str(tmp_path / "cache"))
    return key


@pytest.mark.asyncio
async def test_actual_container_shares_package_references_without_runtime_gate(tmp_path, isolated_composition):
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from omnibci.bootstrap import build_container
    from tests.infrastructure.test_package_store import package_archive

    container = await build_container()
    try:
        packages = container.plugin_package_service
        assert not hasattr(packages, "profiles")
        assert container.configuration_service.change_guard() is container.package_owner
        assert packages.store.plugin_dir == tmp_path / "plugins"
        assert container.runtime_configuration.reference_guard() is container.package_owner
        assert container.plugin_market_service is None
        assert container.engine_service.is_initialized and not container.engine_service.is_engine_ready
        assert container.engine_service._engine is None

        installed = packages.install(package_archive(tmp_path)).package
        assert not installed.enabled and packages.list() == [installed]
        with container.engine_service.admission.command("processing"):
            enabled = packages.set_enabled(installed.package_id, installed.version, True)
        assert enabled.enabled
        profiles = container.configuration_service
        source = profiles.create("effector", installed.contributions[0].selection, "Output", {})
        packages.install(package_archive(tmp_path, "2.0.0"))
        proposal = profiles.propose_profile(source.profile_id, "2.0.0")
        assert not proposal.target_enabled
        packages.set_enabled(installed.package_id, "2.0.0", True)
        copied = profiles.create_version_profile(source.profile_id, "2.0.0", expected_source_revision=source.revision)
        assert copied.selection.version == "2.0.0" and copied.profile_id != source.profile_id
        with pytest.raises(PluginPackageConflictError, match="referenced"):
            packages.set_enabled(installed.package_id, "2.0.0", False)
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_queued_current_configuration_pins_its_package_before_execution(tmp_path, isolated_composition):
    from uuid import uuid4
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from omnibci.bootstrap import build_container
    from tests.infrastructure.test_package_store import package_archive

    container = await build_container()
    try:
        packages = container.plugin_package_service
        installed = packages.install(package_archive(tmp_path)).package
        packages.set_enabled(installed.package_id, installed.version, True)
        selection = installed.contributions[0].selection
        profiles = container.configuration_service
        runtime = container.runtime_configuration
        assert profiles.repository.list_profiles() == []
        assert profiles.repository.get_module("actuation") is None
        plan = runtime.plan("actuation", [{"slot": "actuation.effector", "selection": selection.to_dict(),
                                           "parameters": {"gain": 2.0}}],
                            settings=profiles.default_module_settings("actuation"), expected_revision=None)
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        assert operation["status"] == "queued"
        assert runtime.pending_package_snapshots
        with pytest.raises(PluginPackageConflictError, match="referenced"):
            packages.set_enabled(installed.package_id, installed.version, False)
        await runtime.wait_for_idle()
        assert runtime.operation(operation["operation_id"])["status"] == "succeeded"
        assert runtime.pending_package_snapshots == ()
        assert profiles.repository.get_module("actuation") is not None
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
async def test_operator_configured_market_uses_same_installer_and_admission(monkeypatch, tmp_path, isolated_composition):
    from omnibci.bootstrap import build_container

    _configure_market(monkeypatch, tmp_path)
    container = await build_container()
    try:
        market = container.plugin_market_service
        packages = container.plugin_package_service
        assert market.installation is packages
        assert market.installation.admission_guard == packages.admission_guard
        assert not hasattr(market.repository, "store")
        assert not hasattr(market.repository.repository, "installer")
        assert market.repository.repository.expected_repository_id == "example.plugins"
        assert market.repository.repository.cache_dir == tmp_path / "cache"
        assert not (tmp_path / "cache").exists()
    finally:
        await container.engine_service.shutdown()
        container.package_owner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["stimulation", "actuation"])
@pytest.mark.parametrize("action", ["install", "enable", "remove"])
async def test_node_resources_block_package_mutation_without_blocking_configuration(tmp_path, role, action, isolated_composition):
    from omnibci.application.plugin_management.packages import PluginPackageConflictError
    from omnibci.bootstrap import build_container
    from omnibci.infrastructure.plugin_management.ownership import PluginStoreOwner
    from tests.infrastructure.test_package_store import package_archive

    container = await build_container()
    packages = container.plugin_package_service
    installed = packages.install(package_archive(tmp_path)).package
    node = PluginStoreOwner(packages.store.plugin_dir, role=role)
    try:
        configuration = container.configuration_service
        configuration.save_server({"LOGGING.level": "DEBUG"}, expected_revision=configuration.read_server()[1])
        with pytest.raises(PluginPackageConflictError, match="node owner"):
            if action == "install":
                packages.install(package_archive(tmp_path, "2.0.0"))
            elif action == "enable":
                packages.set_enabled(installed.package_id, installed.version, True)
            else:
                packages.remove(installed.package_id, installed.version)
        assert packages.list() == [installed]
    finally:
        node.close()
        await container.engine_service.shutdown()
        container.package_owner.close()


def test_real_lifespan_routes_install_enable_save_references_and_remove(tmp_path, isolated_composition):
    from time import monotonic, sleep
    from uuid import uuid4
    from fastapi.testclient import TestClient
    from omnibci.presentation.main import create_app
    from tests.infrastructure.test_package_store import package_archive

    app = create_app()
    with TestClient(app) as client:
        container = app.state.container
        assert app.state.plugin_package_service is container.plugin_package_service
        assert app.state.plugin_market_service is None
        assert client.get("/api/v1/market/catalog").status_code == 503
        assert client.post("/api/v1/market/install", json={"package_id": "acme.device", "version": "1.0.0"}).status_code == 503

        response = client.post("/api/v1/plugin-packages/install", content=package_archive(tmp_path).read_bytes(),
                               headers={"Content-Type": "application/zip"})
        assert response.status_code == 201, response.text
        installed = response.json()["data"]["package"]
        assert not installed["enabled"]
        selection = installed["contributions"][0]["selection"]
        assert client.get("/api/v1/plugin-packages").json()["data"] == [installed]
        body = {"kind": "effector", "selection": selection, "name": "Managed output", "parameters": {"gain": 2.0}}
        assert client.post("/api/v1/config/plugin-profiles", json=body).status_code == 422
        path = "/api/v1/plugin-packages/acme.device/1.0.0"
        assert client.put(path + "/enabled", json={"enabled": True}).status_code == 200
        saved = client.post("/api/v1/config/plugin-profiles", json=body)
        assert saved.status_code == 201, saved.text
        profile = saved.json()["data"]
        assert profile["selection"] == selection
        inventory = client.get("/api/v1/plugin-packages").json()["data"][0]
        assert any(ref["scope"] == "profile" and ref["identifier"] == profile["profile_id"] for ref in inventory["references"])
        assert client.put(path + "/enabled", json={"enabled": False}).status_code == 409
        assert client.delete(path).status_code == 409

        def apply(bindings, settings, expected_revision):
            planned = client.post("/api/v1/config/apply-plan", json={
                "module": "actuation", "bindings": bindings, "settings": settings,
                "expected_revision": expected_revision})
            assert planned.status_code == 200, planned.text
            plan = planned.json()["data"]
            assert plan["errors"] == []
            operation_id = str(uuid4())
            response = client.post("/api/v1/config/apply", json={
                "plan_id": plan["plan_id"], "operation_id": operation_id})
            assert response.status_code == 202, response.text
            deadline = monotonic() + 5
            while True:
                operation = client.get(f"/api/v1/config/operations/{operation_id}").json()["data"]
                if operation["status"] not in ("queued", "applying"):
                    assert operation["status"] == "succeeded", operation
                    return client.get("/api/v1/config/modules/actuation").json()["data"]
                assert monotonic() < deadline, operation
                sleep(.01)

        before = client.get("/api/v1/config/modules/actuation").json()["data"]
        original_bindings = [{"slot": item["slot"], "profile_id": item["profile_id"]} for item in before["bindings"]]
        bindings = [item for item in original_bindings if item["slot"] != "actuation.effector"]
        accepted = apply([*bindings, {"slot": "actuation.effector", "profile_id": profile["profile_id"]}],
                         before["settings"], before["revision"])
        inventory = client.get("/api/v1/plugin-packages").json()["data"][0]
        assert {ref["scope"] for ref in inventory["references"]} == {"profile", "accepted"}
        profile_path = f"/api/v1/config/plugin-profiles/{profile['profile_id']}?expected_revision=1"
        assert client.delete(profile_path).status_code == 409
        apply(original_bindings, accepted["settings"], accepted["revision"])
        assert client.delete(profile_path).status_code == 200
        assert client.put(path + "/enabled", json={"enabled": False}).status_code == 200
        assert client.delete(path).status_code == 200
        assert client.get("/api/v1/plugin-packages").json()["data"] == []
        assert container.engine_service._engine is None


def test_lifespan_package_and_profile_mutations_remain_available_during_processing_apply(tmp_path, isolated_composition):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from fastapi.testclient import TestClient
    from omnibci.presentation.main import create_app
    from tests.infrastructure.test_package_store import package_archive

    app = create_app()
    with TestClient(app) as client:
        content = package_archive(tmp_path).read_bytes()
        installed = client.post("/api/v1/plugin-packages/install", content=content, headers={"Content-Type": "application/zip"})
        assert installed.status_code == 201
        selection = installed.json()["data"]["package"]["contributions"][0]["selection"]
        entered, release = Event(), Event()

        def occupied():
            with app.state.container.engine_service.admission.transition("processing"):
                entered.set()
                assert release.wait(5)

        with ThreadPoolExecutor(max_workers=1) as executor:
            operation = executor.submit(occupied)
            try:
                assert entered.wait(2)
                assert client.get("/api/v1/plugin-packages").status_code == 200
                assert client.post("/api/v1/plugin-packages/install", content=content, headers={"Content-Type": "application/zip"}).status_code == 201
                assert client.put("/api/v1/plugin-packages/acme.device/1.0.0/enabled", json={"enabled": True}).status_code == 200
                assert client.post("/api/v1/config/plugin-profiles", json={
                    "kind": "effector", "selection": selection, "name": "Independent output", "parameters": {},
                }).status_code == 201
                assert client.delete("/api/v1/plugin-packages/acme.device/1.0.0").status_code == 409
            finally:
                release.set()
            operation.result(timeout=2)


def test_configured_market_is_published_by_real_lifespan_and_installs_disabled(monkeypatch, tmp_path, isolated_composition):
    from fastapi.testclient import TestClient
    from omnibci.presentation.main import create_app
    from tests.infrastructure.test_market_repository import _DOWNLOAD, _NOW, _Opener, _URL, _index, _signed
    from tests.infrastructure.test_package_store import package_archive

    key = _configure_market(monkeypatch, tmp_path)
    archive = package_archive(tmp_path).read_bytes()
    index = _index(archive)
    index["packages"][0]["domain"] = "actuation"
    index["packages"][0]["contributions"] = [{"id": "output", "kind": "effector"}]
    app = create_app()
    with TestClient(app) as client:
        market = app.state.plugin_market_service
        assert market is app.state.container.plugin_market_service
        repository = market.repository.repository
        repository.clock = lambda: _NOW
        repository._opener = _Opener({_URL: _signed(index, key), _DOWNLOAD: archive})
        catalog = client.get("/api/v1/market/catalog")
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["data"][0]["repository_id"] == "example.plugins"
        installed = client.post("/api/v1/market/install", json={"package_id": "acme.device", "version": "1.0.0"})
        assert installed.status_code == 201, installed.text
        package = installed.json()["data"]["package"]
        assert not package["enabled"] and package["available"]
        assert client.get("/api/v1/plugin-packages").json()["data"] == [package]
        assert market.installation is app.state.plugin_package_service
        assert not list((tmp_path / "plugins").rglob("__pycache__"))


@pytest.mark.parametrize("problem,status", [("invalid", 422), ("unreadable", 503)])
def test_inventory_reports_profile_storage_failures_as_actionable_errors(monkeypatch, problem, status, isolated_composition):
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from omnibci.application.configuration.errors import PluginProfileStorageError
    from omnibci.presentation.main import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        repository = app.state.container.configuration_service.repository
        if problem == "invalid":
            directory = repository.root / "plugin-profiles" / "00000000-0000-0000-0000-000000000001"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "current.toml").write_text("broken = [not valid TOML")
        else:
            monkeypatch.setattr(repository, "list_profiles", Mock(side_effect=PluginProfileStorageError("Profiles unavailable")))
        response = client.get("/api/v1/plugin-packages")
        assert response.status_code == status, response.text
        assert response.json()["success"] is False
