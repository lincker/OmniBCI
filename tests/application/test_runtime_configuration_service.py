"""Configuration reviews pin candidates without mutating selected or running values."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ActivationSet, ModuleActivation, ProfileRevisionConflict, SystemSettings
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.processing_source import processing_input
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan
from tests.plugin_configuration_helpers import accept_profiles, builtin_selection


def prepared_signal_plan(snapshot, *, sample_rate, signal_channel_count):
    channels = tuple(ChannelDescriptor(index, f"EEG{index + 1}", signal_type="eeg") for index in range(signal_channel_count))
    source = SignalInfo(snapshot["ACQUISITION.signal_source"], sample_rate, channels, signal_channel_count,
                        provenance="file-reported")
    return compile_processing_plan(source, processing_input(snapshot) or ProcessingInput())


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    host = {"STIMULATION.auto_spawn": True, "ACTUATION.auto_spawn": True}
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: dict(host))
    snapshot = accept_profiles(profiles, [
        ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 1000, "channel_count": 10}),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
        ("actuation.effector", "effector", "logger", {}),
    ])
    selections = [{"slot": item.slot, "profile_id": item.profile_id} for item in snapshot.bindings()]
    engine = SimpleNamespace(is_engine_ready=True, applied_revisions=snapshot.revisions(),
                             generation="generation-1", applied_snapshot=profiles.materialize(snapshot, "generation-1"),
                             is_peer_connected=lambda role: False, admission=RuntimeAdmission(),
                             restart_blocked=False, runtime_package_snapshots=())
    engine.is_module_running = lambda module: engine.is_engine_ready
    return RuntimeConfigurationService(profiles, engine), selections, snapshot


def edit(runtime, slot, values):
    service, selections, _ = runtime
    profile = service.profiles.repository.get_profile(next(item["profile_id"] for item in selections if item["slot"] == slot))
    return service.profiles.update(profile.profile_id, name=profile.name,
                                   parameters={**profile.parameters, **values}, expected_revision=profile.revision)


def request(runtime, module="processing", *, selections=None, **options):
    service, defaults, _ = runtime
    current = service.profiles.repository.get_module(module) if module != "system" else service.profiles.repository.get_system()
    bindings = [item for item in (defaults if selections is None else selections) if item["slot"].startswith(f"{module}.")]
    return service.plan(module, bindings, expected_revision=current.revision if current else None, **options)


def install(service, candidate):
    current = service.profiles.repository.get_module(candidate.module)
    service.profiles.accept_module(candidate, expected_revision=current.revision if current else None)
    accepted = service.profiles.repository.get_activation_set()
    service.engine.applied_revisions = accepted.revisions()
    service.engine.applied_snapshot = service.profiles.materialize(accepted, service.engine.generation)


def test_plan_is_pinned_and_does_not_apply_saved_values(runtime):
    service, _, before = runtime
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    plan = request(runtime)
    assert plan.strategy == "trial_boundary" and not plan.errors
    edit(runtime, "processing.decoder", {"window_duration": 1.5})
    pinned = service.resolve_plan(plan.plan_id).candidates["processing"]
    assert pinned.bindings[0].parameters["window_duration"] == 2.5
    assert service.profiles.repository.get_activation_set() == before
    assert service.status()["applied"] == before.revisions()


def test_source_change_requires_rebuild(runtime):
    edit(runtime, "acquisition.source", {"sample_rate": 500})
    assert request(runtime, "acquisition").strategy == "rebuild"


def test_source_draft_is_pinned_without_saving_or_changing_profiles(runtime):
    service, _, before = runtime
    profiles = service.profiles.repository.list_profiles()
    source = before.modules["acquisition"].bindings[0]
    draft = {"slot": source.slot, "selection": source.selection.to_dict(),
             "parameters": {"data_file_path": "/tmp/experiment.fif"}}
    plan = request(runtime, "acquisition", selections=[draft])
    assert plan.strategy == "rebuild" and not plan.errors
    assert plan.changed_slots == ("acquisition.source",)
    draft["parameters"]["data_file_path"] = "/tmp/later-edit.fif"
    candidate = service.resolve_plan(plan.plan_id).candidates["acquisition"]
    binding = candidate.bindings[0]
    assert binding.profile_id is None and binding.profile_revision == 0
    assert binding.parameters["data_file_path"] == "/tmp/experiment.fif"
    assert not {"sample_rate", "channel_count"} & binding.parameters.keys()
    assert service.profiles.repository.list_profiles() == profiles
    assert service.profiles.repository.get_activation_set() == before
    install(service, candidate)
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    following = request(runtime)
    assert following.changed_slots == ("processing.decoder",)
    assert service.resolve_plan(following.plan_id).activation_set(service.profiles.repository.get_activation_set()).modules["acquisition"] == candidate


@pytest.mark.parametrize("first_source,summary", [
    ({"source_channel_id": "channel-2"}, "Channels: channel-2 → O2 (eeg), channel-0 → O1"),
    ({"source_channel_index": 2}, "Channels: row 3 → O2 (eeg), channel-0 → O1"),
])
def test_decoder_input_edit_requires_rebuild_and_review_uses_profile_input(runtime, first_source, summary):
    service, selections, before = runtime
    profile = service.profiles.repository.get_profile(selections[1]["profile_id"])
    mapping = ProcessingInput.from_dict({"outputs": [{**first_source, "name": "O2", "signal_type": "eeg"},
                                                    {"source_channel_id": "channel-0", "name": "O1"}]})
    service.profiles.update(profile.profile_id, name=profile.name, parameters=profile.parameters,
                            processing_input=mapping, expected_revision=profile.revision)
    plan = request(runtime)
    assert plan.strategy == "rebuild"
    assert plan.changed_slots == ("processing.decoder",)
    assert plan.changed_settings == () and plan.signal_summary == summary
    candidate = service.resolve_plan(plan.plan_id).candidates["processing"]
    assert candidate.bindings[0].processing_input == mapping
    assert before.modules["processing"].bindings[0].processing_input == ProcessingInput()


def test_decoder_profiles_restore_independent_inputs_and_keep_trial_parameters(runtime):
    service, selections, _ = runtime
    profiles = service.profiles
    selection = builtin_selection("decoder", "THUBCI_SSVEP_classic40CCA")
    first = profiles.create("decoder", selection, "Input A", {},
                            processing_input=ProcessingInput(outputs=(ChannelBinding("O1", "channel-0"),)))
    second = profiles.create("decoder", selection, "Input B", {},
                             processing_input=ProcessingInput(outputs=(ChannelBinding("O2", "channel-1"),)))
    for profile in (first, second, first):
        selections[1] = {"slot": "processing.decoder", "profile_id": profile.profile_id}
        plan = request(runtime)
        assert plan.strategy == "rebuild"
        candidate = service.resolve_plan(plan.plan_id).candidates["processing"]
        assert candidate.bindings[0].processing_input == profile.processing_input
        install(service, candidate)
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    assert request(runtime).strategy == "trial_boundary"


def test_effector_selection_uses_module_boundary(runtime):
    service, selections, _ = runtime
    plan = request(runtime, "actuation", selections=[item for item in selections if item["slot"] != "actuation.effector"])
    assert plan.strategy == "module"
    assert set(plan.target_revisions) == {"actuation"}


def test_idle_plan_does_not_claim_applied_revision(runtime):
    service, _, _ = runtime
    service.engine.is_engine_ready = False
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    plan = request(runtime)
    assert plan.strategy == "select_on_start"
    assert plan.applied_revisions is None and service.status()["applied"] is None


def test_no_semantic_change_does_not_restart(runtime):
    service, _, before = runtime
    plan = request(runtime)
    assert plan.strategy == "no_change"
    assert plan.target_revisions == {"processing": before.modules["processing"].revision}


@pytest.mark.parametrize("module,key,value,strategy", [
    ("system", "NEUROENGINE.queue_size", 123, "rebuild"),
    ("processing", "ALGORITHM.baseline_duration", 3.0, "rebuild"),
    ("actuation", "ACTUATION.watchdog_timeout_s", 3.0, "module"),
    ("system", "debug", True, "rebuild"),
])
def test_runtime_host_changes_use_the_owning_section(runtime, module, key, value, strategy):
    service, _, before = runtime
    service.profiles.settings_source = lambda: {**before.settings(), key: value}
    plan = request(runtime, module)
    assert plan.strategy == strategy and not plan.errors
    assert plan.changed_settings == (key,)
    assert set(plan.target_revisions) == {module}


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("key,value", [("LOGGING.level", "DEBUG"), ("NEUROENGINE.command_timeout_s", 30.0),
                                      ("PLUGINS.user_dir", "/new/plugins")])
def test_changed_backend_startup_settings_cannot_produce_system_plan(runtime, running, key, value):
    service, _, before = runtime
    service.engine.is_engine_ready = running
    service.profiles.settings_source = lambda: {**before.settings(), key: value}
    with pytest.raises(PluginSchemaError, match="Backend restart required"):
        request(runtime, "system")
    assert service.profiles.repository.get_activation_set() == before


def test_saved_other_section_host_changes_are_excluded_from_module_plan(runtime):
    service, _, before = runtime
    service.profiles.settings_source = lambda: {**before.settings(), "NEUROENGINE.queue_size": 30,
                                                "debug": True, "LOGGING.level": "DEBUG"}
    plan = request(runtime)
    assert plan.strategy == "no_change" and plan.changed_settings == ()


def test_restarted_backend_uses_new_startup_baseline(runtime):
    service, _, _ = runtime
    host = {"LOGGING.level": "DEBUG", "debug": True}
    service.profiles = PluginProfileService(service.profiles.repository, service.profiles.catalog,
                                            settings_source=lambda: host, loaded_startup_settings=host)
    assert request(runtime, "system").strategy == "rebuild"


@pytest.mark.parametrize("module", ["stimulation", "actuation"])
@pytest.mark.parametrize("external_before", [False, True])
@pytest.mark.parametrize("connected", [False, True])
def test_external_mode_transitions_check_current_and_target(runtime, module, external_before, connected):
    service, _, before = runtime
    current = before.modules[module]
    settings = {**current.settings, f"{module.upper()}.auto_spawn": not external_before}
    install(service, ModuleActivation(module, settings, current.bindings))
    host = service.profiles.repository.get_activation_set().settings()
    service.profiles.settings_source = lambda: {**host, f"{module.upper()}.auto_spawn": external_before}
    service.engine.is_peer_connected = lambda role: connected and role == module
    plan = request(runtime, module)
    assert plan.strategy == "module"
    if connected:
        assert any("externally managed" in error for error in plan.errors)
        with pytest.raises(PluginSchemaError):
            service.resolve_plan(plan.plan_id)
    else:
        assert not plan.errors
        service.resolve_plan(plan.plan_id)


@pytest.mark.parametrize("slot,parameters,blocked", [
    ("acquisition.source", {"sample_rate": 500}, False),
    ("processing.decoder", {"window_duration": 2.5}, False),
])
def test_external_peer_blocks_only_updates_that_affect_its_runtime(runtime, slot, parameters, blocked):
    service, _, _ = runtime
    service.engine.applied_snapshot["STIMULATION.auto_spawn"] = False
    service.engine.is_peer_connected = lambda role: role == "stimulation"
    edit(runtime, slot, parameters)
    plan = request(runtime, slot.split(".")[0])
    assert any("externally managed" in error for error in plan.errors) is blocked


def test_external_peer_allows_no_change(runtime):
    service, _, _ = runtime
    service.engine.applied_snapshot["STIMULATION.auto_spawn"] = False
    service.engine.is_peer_connected = lambda role: True
    plan = request(runtime)
    assert plan.strategy == "no_change" and not plan.errors


@pytest.mark.parametrize("module,parameters,blocked", [
    ("acquisition", {"sample_rate": 500}, False),
    ("processing", {"window_duration": 2.5}, False),
    ("processing", None, False),
])
def test_external_peer_connecting_after_review_is_rechecked(runtime, module, parameters, blocked):
    service, _, _ = runtime
    service.engine.applied_snapshot["STIMULATION.auto_spawn"] = False
    if parameters is not None:
        slot = "acquisition.source" if module == "acquisition" else "processing.decoder"
        edit(runtime, slot, parameters)
    plan = request(runtime, module)
    assert not plan.errors
    service.engine.is_peer_connected = lambda role: role == "stimulation"
    if blocked:
        with pytest.raises(PluginSchemaError, match="externally managed"):
            service.resolve_plan(plan.plan_id)
    else:
        assert service.resolve_plan(plan.plan_id).plan == plan


@pytest.mark.parametrize("changed", ["generation", "applied", "running", "system"])
def test_stale_runtime_plan_cannot_execute(runtime, changed):
    service, _, before = runtime
    plan = request(runtime)
    if changed == "applied":
        service.engine.applied_revisions["processing"] = "changed"
    elif changed == "system":
        service.profiles.repository.save_system(SystemSettings({"debug": True}), expected_revision=before.system.revision)
    elif changed == "running":
        service.engine.is_engine_ready = False
    else:
        service.engine.generation = "changed"
    with pytest.raises(ProfileRevisionConflict, match="changed"):
        service.resolve_plan(plan.plan_id)


def test_unrelated_module_revision_does_not_expire_processing_plan(runtime):
    service, _, before = runtime
    plan = request(runtime)
    changed = ModuleActivation("actuation", {**before.modules["actuation"].settings, "ACTUATION.watchdog_timeout_s": 3.0},
                               before.modules["actuation"].bindings)
    service.profiles.repository.save_module(changed, expected_revision=before.modules["actuation"].revision)
    service.engine.applied_revisions["actuation"] = changed.revision
    assert service.resolve_plan(plan.plan_id).plan == plan


def test_plan_compare_and_swap_and_expiry(runtime):
    service, selections, _ = runtime
    with pytest.raises(ProfileRevisionConflict):
        service.plan("processing", [item for item in selections if item["slot"].startswith("processing.")], expected_revision="stale")
    plan = request(runtime)
    service._plans[plan.plan_id] = replace(service._plans[plan.plan_id], created_at=-1000)
    with pytest.raises(ProfileRevisionConflict, match="expired"):
        service.resolve_plan(plan.plan_id)


def test_missing_required_source_is_reviewable_error(runtime):
    plan = request(runtime, "acquisition", selections=[])
    assert any("acquisition.source" in error for error in plan.errors)


def test_stopped_acquisition_can_select_an_empty_configuration(runtime):
    service, _, _ = runtime
    service.engine.is_module_running = lambda module: module != "acquisition"
    plan = request(runtime, "acquisition", selections=[])
    assert plan.strategy == "select_on_start"
    assert not plan.errors
    assert service.resolve_plan(plan.plan_id).plan == plan


@pytest.mark.parametrize("role", ["stimulation", "actuation"])
def test_system_change_rechecks_connected_external_outputs(runtime, role):
    service, _, before = runtime
    service.engine.applied_snapshot[f"{role.upper()}.auto_spawn"] = False
    service.profiles.settings_source = lambda: {**before.settings(), "debug": True}
    plan = request(runtime, "system")
    assert not plan.errors
    service.engine.is_peer_connected = lambda selected: selected == role
    with pytest.raises(PluginSchemaError, match="externally managed"):
        service.resolve_plan(plan.plan_id)


def test_filter_order_is_a_real_module_change(runtime):
    service, selections, _ = runtime
    profile = service.profiles.create("filter", builtin_selection("filter", "BASELINE_CORRECTION"), "Baseline", {})
    selections += [{"slot": f"processing.filter.{uuid4()}", "profile_id": profile.profile_id} for _ in range(2)]
    candidate = service.profiles.propose("processing", [item for item in selections if item["slot"].startswith("processing.")])
    install(service, candidate)
    plan = request(runtime, selections=selections[:-2] + list(reversed(selections[-2:])))
    assert plan.strategy == "module"
    assert plan.target_revisions["processing"] != candidate.revision


@pytest.mark.asyncio
async def test_operation_idempotency_owns_one_execution_and_pins_plan(runtime):
    service, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(stored, progress, *, operation_id):
        entered.set()
        progress("starting")
        await release.wait()
        return {"outcome": "applied"}
    executor = SimpleNamespace(supported_strategies={"no_change"}, execute=AsyncMock(side_effect=execute))
    service.executor = executor
    plan = request(runtime)
    operation_id = str(uuid4())
    assert service.submit(plan.plan_id, operation_id)["status"] == "queued"
    await entered.wait()
    assert service.submit(plan.plan_id, operation_id)["status"] == "applying"
    with pytest.raises(ProfileRevisionConflict, match="already"):
        service.submit("different-plan", operation_id)
    with pytest.raises(ProfileRevisionConflict, match="progress"):
        service.submit(plan.plan_id, str(uuid4()))
    release.set()
    await service.wait_for_idle()
    assert service.submit(plan.plan_id, operation_id)["status"] == "succeeded"
    assert executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_failed_operation_keeps_recovery_state_and_rejects_new_apply(runtime):
    from omnibci.application.services.runtime_apply_service import RuntimeApplyFailure
    service, _, _ = runtime
    service.executor = SimpleNamespace(supported_strategies={"no_change"},
        execute=AsyncMock(side_effect=RuntimeApplyFailure("Device outcome unknown", recovery_required=True)))
    plan = request(runtime)
    operation_id = str(uuid4())
    service.submit(plan.plan_id, operation_id)
    await service.wait_for_idle()
    operation = service.operation(operation_id)
    assert operation["status"] == "recovery_required"
    assert service.status()["operation"] == operation
    with pytest.raises(ProfileRevisionConflict, match="uncertain"):
        service.submit(plan.plan_id, str(uuid4()))


def test_nonfilter_binding_reorder_never_claims_no_change_for_different_revision(runtime):
    _, selections, before = runtime
    plan = request(runtime, selections=list(reversed(selections)))
    assert plan.target_revisions["processing"] != before.modules["processing"].revision
    assert plan.strategy == "module"
