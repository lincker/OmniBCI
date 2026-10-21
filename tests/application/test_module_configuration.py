"""Each configuration page owns only its module's selections and validation."""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileBinding
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from tests.application.test_runtime_apply_failures import apply, harness


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
        settings_source=lambda: {},
    )


def source_request(profiles):
    definition = profiles.catalog.legacy("acquisition", "neuroscan_scan45_reader")
    return {"slot": "acquisition.source", "selection": definition.selection.to_dict(),
            "parameters": {"DATA_PROVIDER_IP": "127.0.0.1", "DATA_PROVIDER_PORT": 4000,
                           "sample_rate": 1000, "channel_number": 65}}


def stopped_runtime(profiles):
    engine = SimpleNamespace(is_engine_ready=False, applied_revision=None, generation=None,
                             applied_snapshot=None, restart_blocked=False,
                             runtime_package_snapshots=())
    return RuntimeConfigurationService(profiles, engine)


def test_acquisition_can_be_selected_and_started_without_other_modules(profiles):
    runtime = stopped_runtime(profiles)
    plan = runtime.plan([source_request(profiles)], module="acquisition", expected_revision=None)
    assert not plan.errors
    assert plan.signal_summary is None
    candidate = runtime.resolve_plan(plan.plan_id).snapshot
    profiles.accept(candidate, module="acquisition", expected_revision=None)

    snapshot = accepted_runtime_settings(profiles=profiles)
    assert [item["slot"] for item in snapshot["PLUGINS.bindings"]] == ["acquisition.source"]
    assert snapshot["ACQUISITION.signal_source"] == "neuroscan_scan45_reader"


@pytest.mark.asyncio
async def test_first_acquisition_apply_executes_without_preparing_an_experiment(profiles):
    runtime = stopped_runtime(profiles)

    @asynccontextmanager
    async def transition():
        yield

    runtime.engine.runtime_transition = transition
    acquisition, commands = Mock(), Mock()
    runtime.executor = RuntimeApplyCoordinator(
        runtime.engine, profiles, acquisition,
        SimpleNamespace(is_active=False, process_alive=False), commands,
    )
    plan = runtime.plan([source_request(profiles)], module="acquisition", expected_revision=None)
    operation = runtime.submit(plan.plan_id, str(uuid4()))
    await runtime.wait_for_idle()

    assert runtime.operation(operation["operation_id"])["status"] == "succeeded"
    assert [b.slot for b in profiles.repository.get_activation().bindings] == ["acquisition.source"]
    assert not acquisition.mock_calls and not commands.mock_calls


@pytest.mark.parametrize("problem", ["legacy_mapping", "missing_package", "invalid_parameters"])
def test_acquisition_does_not_inspect_or_modify_dormant_decoder(profiles, monkeypatch, problem):
    definition = profiles.catalog.legacy("decoder", "THUBCI_SSVEP_classic40CCA")
    decoder = ProfileBinding("processing.decoder", "decoder", None, definition.selection,
                             {"window_duration": 1.0, "offset_time": 0.0})
    if problem == "legacy_mapping":
        decoder = replace(decoder, legacy={"processing_input": {
            "schema_version": 1, "outputs": ["O1"],
            "reference": {"mode": "average", "channels": ["O1", "O2"]},
        }})
    elif problem == "missing_package":
        decoder = replace(decoder, selection=replace(decoder.selection, version="not-installed"))
    else:
        decoder = replace(decoder, parameters={"window_duration": "invalid"})
    previous = ActivationSnapshot({"SIGNAL.setup": {"outputs": ["O1"]}}, (decoder,))
    profiles.repository.save_activation(previous, expected_revision=None)
    get = profiles.catalog.get

    def only_acquisition(kind, selection):
        assert kind == "acquisition", "Acquisition must not resolve another module's plugin"
        return get(kind, selection)

    monkeypatch.setattr(profiles.catalog, "get", only_acquisition)
    runtime = stopped_runtime(profiles)
    plan = runtime.plan([source_request(profiles)], module="acquisition",
                        expected_revision=previous.revision)
    assert not plan.errors
    assert plan.changed_slots == ("acquisition.source",)
    candidate = runtime.resolve_plan(plan.plan_id).snapshot
    assert next(b for b in candidate.bindings if b.kind == "decoder") == decoder
    profiles.accept(candidate, module="acquisition", expected_revision=previous.revision)
    snapshot = accepted_runtime_settings(profiles=profiles)
    assert "SIGNAL.setup" not in snapshot
    assert next(b for b in snapshot["PLUGINS.bindings"]
                if b["slot"] == "processing.decoder") == decoder.to_dict()


@pytest.mark.parametrize("module,slot,kind,alias", [
    ("processing", "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("stimulation", "stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("actuation", "actuation.effector", "effector", "logger"),
])
def test_each_module_can_be_configured_first(profiles, module, slot, kind, alias):
    definition = profiles.catalog.legacy(kind, alias)
    profile = profiles.create(kind, definition.selection, alias, {})
    runtime = stopped_runtime(profiles)
    plan = runtime.plan([{"slot": slot, "profile_id": profile.profile_id}],
                        module=module, expected_revision=None)
    assert not plan.errors
    profiles.accept(runtime.resolve_plan(plan.plan_id).snapshot, module=module,
                    expected_revision=None)
    assert [b.slot for b in profiles.repository.get_activation().bindings] == [slot]


def test_module_request_cannot_change_another_modules_slots(profiles):
    with pytest.raises(PluginSchemaError, match="module"):
        profiles.propose([source_request(profiles)], module="processing")


def test_acquisition_still_validates_its_own_parameters(profiles):
    request = source_request(profiles)
    request["parameters"]["DATA_PROVIDER_PORT"] = "invalid"
    with pytest.raises(PluginSchemaError):
        stopped_runtime(profiles).plan([request], module="acquisition", expected_revision=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("actuation_enabled", [False, True])
async def test_connected_source_reconfiguration_does_not_prepare_other_modules(harness, actuation_enabled):
    h = harness(actuation=actuation_enabled)
    h.acquisition.streaming = False
    h.engine.actuation_process_alive = False
    h.runtime.executor.signal_preparer = Mock(side_effect=AssertionError("Processing is not in use"))
    decoder = next(b for b in h.previous.bindings if b.kind == "decoder")
    dormant = replace(decoder, processing_input=None, legacy={
        "processing_input": {"schema_version": 1, "outputs": ["O1"]},
    })
    previous = ActivationSnapshot(h.previous.settings, tuple(
        dormant if b.kind == "decoder" else b for b in h.previous.bindings
    ))
    h.profiles.repository.save_activation(previous, expected_revision=h.previous.revision)
    h.engine.applied_snapshot = {**h.profiles.materialize(previous), "CONFIG.generation": h.engine.generation}
    source = next(b for b in previous.bindings if b.kind == "acquisition")
    h.plan = h.runtime.plan([{"slot": source.slot, "profile_id": source.profile_id}],
                            module="acquisition", expected_revision=previous.revision)
    assert not h.plan.errors

    operation = await apply(h)

    assert operation["status"] == "succeeded", operation
    assert h.acquisition.connected and not h.acquisition.streaming
    assert h.runtime.executor.signal_preparer.call_count == 0
    assert not any(event[0] == "SettleActuation" for event in h.log)
    assert next(b for b in h.profiles.repository.get_activation().bindings
                if b.kind == "decoder") == dormant


def test_disconnected_external_stimulation_does_not_block_acquisition_apply(profiles):
    candidate = profiles.propose([source_request(profiles)], module="acquisition")
    candidate = ActivationSnapshot({"STIMULATION.auto_spawn": False}, candidate.bindings)
    profiles.accept(candidate, expected_revision=None, module="acquisition")
    engine = SimpleNamespace(
        is_engine_ready=True, applied_revision=candidate.revision, generation="running",
        applied_snapshot=profiles.materialize(candidate), is_peer_connected=lambda role: False,
    )
    request = source_request(profiles)
    request["parameters"]["DATA_PROVIDER_PORT"] = 4001
    runtime = RuntimeConfigurationService(profiles, engine)
    plan = runtime.plan([request], module="acquisition", expected_revision=candidate.revision)
    assert not plan.errors
    assert plan.module == "acquisition"
    assert plan.strategy == "rebuild"
