"""Runtime plugin resolution, output plugin loading, and module runtime settings."""
import asyncio
from dataclasses import replace
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.domain.ports.acquisition import TriggerEmitter
from omnibci.infrastructure.actuation.actuator import Actuator
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
from omnibci.infrastructure.plugin_management.loader import PluginLoadError, PluginLoader
from omnibci.infrastructure.plugin_management.preparation import configured_bindings
from omnibci.infrastructure.plugin_management.runtime import load_configured_plugin
from omnibci.infrastructure.stimulation.stimulator import Stimulator
from omnibci.sdk import Effector, Paradigm, SignalSource
from tests.application.test_signal_setup_service import signal_service  # noqa: F401
from tests.execution_helpers import synthetic_execution_snapshot
from tests.infrastructure.test_child_runtime_snapshot import MANAGERS, _mock_spawning, _snapshot
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot


@pytest.mark.parametrize("module,kind", [("stimulation", "paradigm"), ("actuation", "effector")])
@pytest.mark.parametrize("sample_rate", [0, "invalid", None])
def test_output_inputs_do_not_depend_on_acquisition(module, kind, sample_rate):
    alias = "logger" if kind == "effector" else "STIMULATION_THUBCI_SSVEP_classic40"
    binding = ProfileBinding(f"{module}.{kind}", kind, None, builtin_selection(kind, alias), {})
    loader = Mock()
    load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()],
                            "ACQUISITION.sample_rate": sample_rate,
                            "CONFIG.module_revisions": {module: "output-revision"}}),
                           binding.slot, kind, Effector if kind == "effector" else Paradigm,
                           loader=loader)
    assert loader.instantiate.call_args.kwargs == {"parameters": {}}


def _source_binding():
    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "file_reader"))
    return ProfileBinding("acquisition.source", "acquisition", None, descriptor.selection, {})


def test_missing_selected_version_never_falls_back_to_alias():
    binding = _source_binding()
    binding = replace(binding, selection=replace(binding.selection, version="999.0.0"))
    with pytest.raises(PluginLoadError, match="Unknown"):
        load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()],
                                "ACQUISITION.signal_source": "file_reader"}),
                               "acquisition.source", "acquisition", SignalSource)


def test_duplicate_slots_are_rejected():
    binding = _source_binding().to_dict()
    with pytest.raises(PluginLoadError, match="Duplicate"):
        configured_bindings({"PLUGINS.bindings": [binding, binding]})
    with pytest.raises(PluginLoadError, match="Duplicate"):
        load_configured_plugin({"PLUGINS.bindings": [binding, binding]},
                               "acquisition.source", "acquisition", SignalSource)


def test_source_loading_does_not_parse_dormant_decoder_input():
    binding = replace(_source_binding(), parameters={"data_file_path": "selected.cnt"})
    malformed_decoder = {
        "slot": "processing.decoder", "kind": "decoder",
        "selection": {"package_id": "missing.decoder", "contribution_id": "default"},
        "parameters": {}, "configuration_version": 1, "profile_revision": 0,
        "processing_input": {"schema_version": 2, "outputs": "invalid"},
    }
    source = load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict(), malformed_decoder]}, ("acquisition",)),
                                    "acquisition.source", "acquisition", SignalSource)
    assert source.file_path.endswith("selected.cnt")


def test_schema_version_requires_migration_before_loading():
    binding = replace(_source_binding(), configuration_version=99)
    with pytest.raises(PluginLoadError, match="migration required"):
        load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [binding.to_dict()]}),
                               "acquisition.source", "acquisition", SignalSource)


def _output_binding(slot, kind, alias):
    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    return ProfileBinding(slot, kind, None, descriptor.selection, {}).to_dict()


def test_selected_paradigm_loads_without_borrowing_hardware():
    snapshot = {"PLUGINS.bindings": [_output_binding("stimulation.paradigm", "paradigm",
                                                     "STIMULATION_THUBCI_SSVEP_classic40")]}
    stimulator = Stimulator(settings_snapshot=prepared_snapshot(snapshot, ("stimulation",)))
    paradigm = stimulator._load_paradigm()
    assert paradigm.trigger is None


def test_stimulation_without_selected_trigger_starts_and_clears_without_hardware(monkeypatch):
    snapshot = {"PLUGINS.bindings": [_output_binding("stimulation.paradigm", "paradigm",
                                                     "STIMULATION_THUBCI_SSVEP_classic40")]}
    stimulator = Stimulator(settings_snapshot=prepared_snapshot(snapshot, ("stimulation",)))
    trigger = stimulator._load_trigger()
    assert isinstance(trigger, TriggerEmitter)
    assert trigger.setup() is None
    assert trigger.send(3) is None
    assert trigger.clear() is None
    paradigm = stimulator._load_paradigm()
    assert paradigm.trigger is None
    paradigm.setup = Mock(return_value=None)
    monkeypatch.setattr(stimulator, "_load_paradigm", Mock(return_value=paradigm))
    renderer, loop = Mock(), Mock()
    renderer.initialize.return_value = renderer.shutdown.return_value = None
    loop.run_async = AsyncMock()
    create_renderer = Mock(return_value=renderer)
    monkeypatch.setattr("omnibci.infrastructure.stimulation.stimulator._create_renderer", create_renderer)
    monkeypatch.setattr("omnibci.infrastructure.stimulation.stimulator.RenderLoop", Mock(return_value=loop))

    asyncio.run(stimulator.start_async())
    create_renderer.assert_called_once_with("moderngl")
    loop.run_async.assert_awaited_once()
    assert paradigm.setup.call_args.kwargs["renderer"] is renderer
    assert isinstance(paradigm.setup.call_args.kwargs["trigger"], TriggerEmitter)
    renderer.shutdown.assert_called_once()
    assert stimulator.trigger is None


@pytest.mark.parametrize("snapshot", [{}, {"PLUGINS.bindings": None}, {"PLUGINS.bindings": [{}]}])
def test_optional_trigger_still_requires_valid_pinned_bindings(snapshot):
    with pytest.raises((PluginLoadError, ValueError)):
        Stimulator(settings_snapshot=snapshot)._load_trigger()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_selected_trigger_platform_errors_are_not_replaced_by_empty_output(monkeypatch, platform):
    snapshot = {"PLUGINS.bindings": [_output_binding("stimulation.trigger", "trigger", "neuroscan_scan45_trigger")]}
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(PluginLoader, "_prepare_module", Mock(side_effect=AssertionError("hardware import")))
    with pytest.raises(PluginLoadError, match=f"not supported on {platform}"):
        Stimulator(settings_snapshot=prepared_snapshot(snapshot, ("stimulation",)))._load_trigger()


def test_selected_trigger_loading_errors_are_not_replaced_by_empty_output(monkeypatch):
    snapshot = {"PLUGINS.bindings": [_output_binding("stimulation.trigger", "trigger", "neuroscan_scan45_trigger")]}
    monkeypatch.setattr("omnibci.infrastructure.plugin_management.runtime.load_configured_plugin",
                        Mock(side_effect=OSError("trigger initialization failed")))
    with pytest.raises(OSError, match="trigger initialization failed"):
        Stimulator(settings_snapshot=snapshot)._load_trigger()


def test_selected_effector_keeps_host_revision_private():
    snapshot = {"PLUGINS.bindings": [_output_binding("actuation.effector", "effector", "logger")],
                "CONFIG.module_revisions": {"actuation": "selected-revision"}}
    actuator = Actuator(settings_snapshot=prepared_snapshot(snapshot, ("actuation",)))
    actuator.setup()
    assert not hasattr(actuator.effector, "module_revision")
    assert actuator._settings_snapshot["CONFIG.module_revisions"]["actuation"] == "selected-revision"
    actuator.execute({"target_id": 1})
    actuator.teardown()
    assert actuator.effector is None


def test_trigger_constructor_does_not_receive_host_revision(monkeypatch):
    selected = _output_binding("stimulation.trigger", "trigger", "neuroscan_scan45_trigger")
    descriptor = PluginCatalog().resolve_selection("trigger", builtin_selection("trigger", "neuroscan_scan45_trigger"))
    assert descriptor.package.domain == "acquisition"
    monkeypatch.setattr(sys, "platform", "win32")
    loader = Mock()
    load_configured_plugin(prepared_snapshot({"PLUGINS.bindings": [selected], "CONFIG.module_revisions": {
        "acquisition": "source-revision", "stimulation": "stimulation-revision",
    }}), "stimulation.trigger", "trigger", TriggerEmitter, loader=loader)
    assert set(loader.instantiate.call_args.kwargs) == {"parameters"}


def _assert_scope(value, module):
    modules = {module, "acquisition"} if module == "processing" else {module}
    assert set(value["CONFIG.module_revisions"]) == modules
    assert all(binding["slot"].split(".", 1)[0] in modules for binding in value["PLUGINS.bindings"])
    assert all(slot.startswith(module + ".") for slot in value.get("PLUGINS.artifacts", {}))
    assert "LOGGING.file_path" not in value and "PLUGINS.user_dir" not in value
    for other in {"stimulation", "actuation"} - modules:
        assert not any(key.startswith(other.upper() + ".") for key in value)


@pytest.mark.parametrize("module", ["acquisition", "processing"])
def test_worker_factory_passes_only_owned_values_and_required_source_context(module):
    snapshot = {
        "CONFIG.module_revisions": {name: "a" * 64 for name in ("acquisition", "processing", "stimulation", "actuation")},
        "CONFIG.engine_revision": "b" * 64, "CONFIG.generation": "generation",
        "ACQUISITION.sample_rate": 500,
        "ALGORITHM.baseline_duration": 0.1, "STIMULATION.server_port": 9001,
        "ACTUATION.server_port": 9002, "LOGGING.file_path": "server-private",
        "PLUGINS.user_dir": "store-private", "debug": False,
        "PLUGINS.bindings": [ProfileBinding(slot, kind, None,
            PluginSelection("test." + kind, "default", "1.0.0"), {"value": 1}).to_dict()
            for slot, kind in (("acquisition.source", "acquisition"), ("processing.decoder", "decoder"),
                               ("stimulation.paradigm", "paradigm"), ("actuation.effector", "effector"))],
    }
    snapshot = synthetic_execution_snapshot(snapshot)
    engine = NeuroEngineBuilder(settings_snapshot={}).build()
    channel = None
    try:
        channel = engine._worker_factory(module, snapshot)
        value = channel.process.settings_snapshot
        _assert_scope(value, module)
        assert value["CONFIG.generation"] == "generation"
        assert value["CONFIG.engine_revision"] == "b" * 64
        assert value["ACQUISITION.sample_rate"] == 500
        value["PLUGINS.bindings"][0]["parameters"]["value"] = 2
        assert snapshot["PLUGINS.bindings"][0]["parameters"]["value"] == 1
    finally:
        if channel is not None:
            channel.receiver.close()
            channel.reporter.close()
        asyncio.run(engine._shutdown_resources())


@pytest.mark.parametrize(("module", "manager_class", "component", "key"), MANAGERS)
def test_managed_output_process_receives_only_its_owned_configuration(monkeypatch, module, manager_class, component, key):
    factory = _mock_spawning(monkeypatch, module, component)
    manager = manager_class()
    snapshot = _snapshot(key)
    role = component.lower()
    snapshot["PLUGINS.bindings"][0].setdefault("slot", role + ".effector")
    snapshot["PLUGINS.bindings"].append({"slot": "acquisition.source", "parameters": {"private": "source"}})
    snapshot["PLUGINS.user_dir"] = "controller-private"
    snapshot["LOGGING.file_path"] = "controller-log"
    try:
        manager.start(settings_snapshot=snapshot)
        value = factory.call_args.kwargs["kwargs"]["settings_snapshot"]
        _assert_scope(value, role)
        assert value["CONFIG.generation"] == "graph-1"
    finally:
        manager.stop()


def test_signal_preparation_does_not_send_output_settings_or_artifacts(signal_service):
    service, _, _, calls = signal_service
    snapshot = service.engine.applied_snapshot
    snapshot["CONFIG.module_revisions"].update(stimulation="s", actuation="a")
    snapshot["PLUGINS.bindings"].append({"slot": "actuation.effector", "parameters": {"private": "device"}})
    snapshot["PLUGINS.artifacts"] = {"processing.decoder": {"location": "decoder"},
                                     "actuation.effector": {"location": "effector"}}
    snapshot["ACTUATION.server_port"] = 9001
    service.prepare()
    value = calls[0][1]["settings_snapshot"]
    _assert_scope(value, "processing")
    assert value["PLUGINS.artifacts"] == {"processing.decoder": {"location": "decoder"}}
    assert snapshot["PLUGINS.bindings"][-1]["slot"] == "actuation.effector"


@pytest.mark.asyncio
async def test_pipeline_update_does_not_send_output_configuration():
    from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator

    snapshot = {
        "CONFIG.module_revisions": {"acquisition": "a", "processing": "p", "stimulation": "s", "actuation": "e"},
        "CONFIG.generation": "g", "CONFIG.engine_revision": "engine",
        "PLUGINS.bindings": [{"slot": "processing.decoder"}, {"slot": "actuation.effector"}],
        "PLUGINS.artifacts": {"processing.decoder": {}, "actuation.effector": {}},
        "STIMULATION.server_port": 9001, "ACTUATION.server_port": 9002,
    }
    coordinator = RuntimeApplyCoordinator.__new__(RuntimeApplyCoordinator)
    calls = []

    async def request(event_type, payload):
        calls.append(payload)
        return {"status": "applied", "applied_revision": "p", "engine_revision": "engine", "generation": "g"}

    coordinator._request = request
    await coordinator._stage_pipeline(snapshot, "old")
    _assert_scope(calls[0]["settings_snapshot"], "processing")
