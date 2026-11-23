"""Process and command boundaries disclose only owned module configuration."""
import asyncio

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_management.identity import PluginSelection
from tests.execution_helpers import synthetic_execution_snapshot

from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from tests.application.test_signal_setup_service import signal_service
from tests.infrastructure.test_child_runtime_snapshot import MANAGERS, _mock_spawning, _snapshot


def assert_scope(value, module):
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
        "ACQUISITION.signal_source": "source", "ACQUISITION.sample_rate": 500,
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
        assert_scope(value, module)
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
        assert_scope(value, role)
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
    assert_scope(value, "processing")
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
    assert_scope(calls[0]["settings_snapshot"], "processing")
