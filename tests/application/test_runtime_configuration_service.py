"""Configuration reviews pin candidates without mutating selected or running values."""
from tests.plugin_configuration_helpers import section_settings
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnibci.application.configuration.models import ModuleActivation, EngineSettings
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.plugin_management.schema import PluginSchemaError
from omnibci.application.processing_source import processing_input
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan
from tests.plugin_configuration_helpers import accept_profiles, builtin_selection


def prepared_signal_plan(snapshot, *, sample_rate, signal_channel_count):
    channels = tuple(ChannelDescriptor(index, f"EEG{index + 1}", signal_type="eeg", channel_id=f"channel-{index}") for index in range(signal_channel_count))
    source = SignalInfo(sample_rate=sample_rate, channels=channels)
    return compile_processing_plan(source, processing_input(snapshot) or ProcessingInput())


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter())
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
                             restart_blocked=False, foundation_requires_cleanup=False, runtime_package_snapshots=())
    engine.is_module_running = lambda module: engine.is_engine_ready
    return RuntimeConfigurationService(profiles, engine), selections, snapshot


def edit(runtime, slot, values):
    service, selections, _ = runtime
    profile = service.profiles.repository.get_profile(next(item["profile_id"] for item in selections if item["slot"] == slot))
    return service.profiles.update(profile.profile_id, name=profile.name,
                                   parameters={**profile.parameters, **values}, expected_revision=profile.revision)


def request(runtime, module="processing", *, selections=None, **options):
    service, defaults, _ = runtime
    current = service.profiles.repository.get_module(module) if module != "engine" else service.profiles.repository.get_engine()
    bindings = [item for item in (defaults if selections is None else selections) if item["slot"].startswith(f"{module}.")]
    settings = options.pop("settings", current.settings if current else service.profiles.default_module_settings(module))
    return service.plan(module, bindings, expected_revision=current.revision if current else None, settings=settings, **options)


def install(service, candidate):
    current = service.profiles.repository.get_module(candidate.module)
    service.profiles.repository.save_module(candidate, expected_revision=current.revision if current else None)
    accepted = service.profiles.selected_configuration()
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
    assert service.profiles.selected_configuration() == before
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
    assert service.profiles.selected_configuration() == before
    install(service, candidate)
    edit(runtime, "processing.decoder", {"window_duration": 2.5})
    following = request(runtime)
    assert following.changed_slots == ("processing.decoder",)
    assert service.resolve_plan(following.plan_id).activation_set(service.profiles.selected_configuration()).modules["acquisition"] == candidate


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
    ("engine", "NEUROENGINE.queue_size", 123, "rebuild"),
    ("processing", "ALGORITHM.baseline_duration", 3.0, "rebuild"),
    ("actuation", "ACTUATION.watchdog_timeout_s", 3.0, "module"),
    ("engine", "debug", True, "rebuild"),
])
def test_runtime_host_changes_use_the_owning_section(runtime, module, key, value, strategy):
    service, _, before = runtime
    plan = request(runtime, module, settings=section_settings({**before.settings(), key: value}, module))
    assert plan.strategy == strategy and not plan.errors
    assert plan.changed_settings == (key,)
    assert set(plan.target_revisions) == {module}


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("key,value", [("LOGGING.level", "DEBUG"), ("NEUROENGINE.command_timeout_s", 30.0),
                                      ("PLUGINS.user_dir", "/new/plugins")])
def test_changed_server_startup_settings_do_not_block_engine_plan(runtime, running, key, value):
    service, _, before = runtime
    service.engine.is_engine_ready = running
    service.profiles.save_server({key: value}, expected_revision=service.profiles.read_server()[1])
    plan = request(runtime, "engine")
    assert not plan.errors
    assert key not in service.resolve_plan(plan.plan_id).engine.settings
    assert service.profiles.selected_configuration() == before


def test_saved_other_section_host_changes_are_excluded_from_module_plan(runtime):
    service, _, before = runtime
    service.profiles.repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 30, "debug": True}),
                                            expected_revision=before.engine.revision)
    service.profiles.save_server({"LOGGING.level": "DEBUG"}, expected_revision=service.profiles.read_server()[1])
    plan = request(runtime)
    assert plan.strategy == "no_change" and plan.changed_settings == ()


@pytest.mark.parametrize("module", ["stimulation", "actuation"])
@pytest.mark.parametrize("external_before", [False, True])
@pytest.mark.parametrize("connected", [False, True])
def test_external_mode_transitions_check_current_and_target(runtime, module, external_before, connected):
    service, _, before = runtime
    current = before.modules[module]
    settings = {**current.settings, f"{module.upper()}.auto_spawn": not external_before}
    install(service, ModuleActivation(module, settings, current.bindings))
    service.engine.is_peer_connected = lambda role: connected and role == module
    plan = request(runtime, module, settings={**current.settings, f"{module.upper()}.auto_spawn": external_before})
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


@pytest.mark.parametrize("changed", ["generation", "applied", "running", "engine"])
def test_stale_runtime_plan_cannot_execute(runtime, changed):
    service, _, before = runtime
    plan = request(runtime)
    if changed == "applied":
        service.engine.applied_revisions["processing"] = "changed"
    elif changed == "engine":
        service.profiles.repository.save_engine(EngineSettings({"debug": True}), expected_revision=before.engine.revision)
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
        service.plan("processing", [item for item in selections if item["slot"].startswith("processing.")], expected_revision="stale", settings=service.profiles.default_module_settings("processing"))
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
def test_engine_change_rechecks_connected_external_outputs(runtime, role):
    service, _, before = runtime
    service.engine.applied_snapshot[f"{role.upper()}.auto_spawn"] = False
    plan = request(runtime, "engine", settings={**before.engine.settings, "debug": True})
    assert not plan.errors
    service.engine.is_peer_connected = lambda selected: selected == role
    with pytest.raises(PluginSchemaError, match="externally managed"):
        service.resolve_plan(plan.plan_id)


def test_filter_order_is_a_real_module_change(runtime):
    service, selections, _ = runtime
    profile = service.profiles.create("filter", builtin_selection("filter", "BASELINE_CORRECTION"), "Baseline", {})
    selections += [{"slot": f"processing.filter.{uuid4()}", "profile_id": profile.profile_id} for _ in range(2)]
    candidate = service.profiles.propose("processing", [item for item in selections if item["slot"].startswith("processing.")], settings=service.profiles.default_module_settings("processing"))
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
    with pytest.raises(ProfileRevisionConflict, match="owns"):
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
    assert service.status()["operations"]["processing"] == operation
    with pytest.raises(ProfileRevisionConflict, match="uncertain"):
        service.submit(plan.plan_id, str(uuid4()))


@pytest.mark.asyncio
async def test_modules_apply_concurrently_and_queued_work_owns_its_scope(runtime):
    service, _, _ = runtime
    entered = {module: asyncio.Event() for module in ("processing", "stimulation")}
    release = {module: asyncio.Event() for module in entered}

    async def execute(stored, progress, *, operation_id):
        module = stored.plan.module
        entered[module].set()
        await release[module].wait()
        return {"outcome": "applied"}

    service.executor = SimpleNamespace(execute=execute)
    plans = {module: request(runtime, module) for module in entered}
    operations = {module: service.submit(plan.plan_id, str(uuid4())) for module, plan in plans.items()}
    with pytest.raises(ProfileRevisionConflict):
        with service.engine.admission.command("processing"):
            pass
    await asyncio.gather(*(event.wait() for event in entered.values()))
    release["stimulation"].set()
    await asyncio.sleep(0)
    assert service.operation(operations["stimulation"]["operation_id"])["status"] == "succeeded"
    assert service.operation(operations["processing"]["operation_id"])["status"] == "applying"
    assert len(service.pending_package_snapshots) == 1
    release["processing"].set()
    await service.wait_for_idle()
    assert service.pending_package_snapshots == ()


@pytest.mark.asyncio
async def test_module_recovery_preserves_candidate_packages_until_confirmed_stop(runtime):
    from omnibci.application.services.runtime_apply_service import RuntimeApplyFailure
    service, _, _ = runtime
    service.executor = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeApplyFailure(
        "Pipeline outcome unknown", recovery_required=True)))
    plan = request(runtime)
    service.submit(plan.plan_id, str(uuid4()))
    await service.wait_for_idle()
    assert service.status()["recovery"] == {"processing": "Pipeline outcome unknown"}
    assert service.pending_package_snapshots
    service.executor = SimpleNamespace(execute=AsyncMock(return_value={"outcome": "no_change"}))
    independent = request(runtime, "stimulation")
    service.submit(independent.plan_id, str(uuid4()))
    await service.wait_for_idle()
    assert service.pending_package_snapshots
    service.engine.admission.clear_quarantine("processing")
    assert service.pending_package_snapshots == ()


def test_unrelated_corrupt_configuration_is_reported_without_blocking_module_plan(runtime, monkeypatch):
    service, _, _ = runtime
    get_module = service.profiles.repository.get_module

    def read(module):
        if module == "processing":
            raise ValueError("Invalid Processing configuration")
        return get_module(module)

    monkeypatch.setattr(service.profiles.repository, "get_module", read)
    plan = request(runtime, "stimulation")
    assert service.resolve_plan(plan.plan_id).plan == plan
    assert service.status()["errors"] == {"processing": "Invalid Processing configuration"}


@pytest.mark.parametrize("module,strategy", [("acquisition", "select_on_start"),
                                             ("processing", "select_on_start"),
                                             ("stimulation", "module"), ("actuation", "module")])
def test_saved_module_can_be_prepared_after_foundation_only_start(runtime, module, strategy):
    service, _, _ = runtime
    service.engine.applied_revisions[module] = None
    service.engine.applied_snapshot["CONFIG.module_revisions"][module] = None
    service.engine.is_module_running = lambda selected: selected != module
    plan = request(runtime, module)
    assert not plan.errors
    assert plan.strategy == strategy
    assert plan.applied_revisions[module] is None
    assert service.resolve_plan(plan.plan_id).plan == plan


def test_nonfilter_binding_reorder_never_claims_no_change_for_different_revision(runtime):
    _, selections, before = runtime
    plan = request(runtime, selections=list(reversed(selections)))
    assert plan.target_revisions["processing"] != before.modules["processing"].revision
    assert plan.strategy == "module"


@pytest.fixture
def current_runtime(tmp_path, monkeypatch):
    from omnibci.application.services.engine_service import NeuroEngineService
    from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator

    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter())
    engine = NeuroEngineService(lambda _: pytest.fail("Selecting current values must not start an engine"))
    coordinator = RuntimeApplyCoordinator(engine, profiles, None,
                                          SimpleNamespace(is_active=False, process_alive=False), None, repository=profiles.repository)
    return RuntimeConfigurationService(profiles, engine, executor=coordinator)


def _draft(rt, slot, kind, alias, **extra):
    definition = rt.profiles.catalog.get(kind, builtin_selection(kind, alias))
    return {"slot": slot, "selection": definition.selection.to_dict(), "parameters": {},
            "configuration_version": definition.configuration_version,
            **({"processing_input": {"schema_version": 2, "outputs": []}} if kind == "decoder" else {}), **extra}


async def _apply_current(rt, bindings, module):
    previous = rt.profiles.repository.get_module(module)
    plan = rt.plan(module, bindings, settings={}, expected_revision=previous.revision if previous else None)
    assert not plan.errors
    operation = rt.submit(plan.plan_id, str(uuid4()))
    await rt.wait_for_idle()
    result = rt.operation(operation["operation_id"])
    assert result["status"] == "succeeded", result
    return rt.profiles.repository.get_module(module)


@pytest.mark.parametrize("slot,kind,alias", [
    ("acquisition.source", "acquisition", "file_reader"),
    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
    ("processing.filter.00000000-0000-0000-0000-000000000001", "filter", "BASELINE_CORRECTION"),
    ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("stimulation.trigger", "trigger", "neuroscan_scan45_trigger"),
    ("actuation.effector", "effector", "logger"),
])
@pytest.mark.asyncio
async def test_apply_current_values_never_requires_or_creates_a_profile(current_runtime, slot, kind, alias):
    current = _draft(current_runtime, slot, kind, alias)
    selected = await _apply_current(current_runtime, [current], slot.split(".")[0])
    binding, = selected.bindings
    assert binding.kind == kind
    assert binding.profile_id is None
    assert binding.profile_revision == 0
    assert binding.selection.to_dict() == current["selection"]
    assert current_runtime.profiles.repository.list_profiles() == []
    assert current_runtime.status()["accepted"][slot.split(".")[0]] == selected.revision
    assert current_runtime.status()["applied"] is None


@pytest.mark.asyncio
async def test_processing_current_values_pin_mapping_and_filter_order_without_changing_source(current_runtime):
    source = await _apply_current(current_runtime, [_draft(current_runtime, "acquisition.source", "acquisition", "file_reader")],
                                  "acquisition")
    mapping = {"schema_version": 2, "outputs": [
        {"source_channel_index": 2, "name": "Right", "signal_type": "eeg"},
        {"source_channel_index": 0, "name": "Left", "signal_type": "eeg"},
    ]}
    decoder = _draft(current_runtime, "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA",
                     processing_input=mapping)
    filters = [_draft(current_runtime, f"processing.filter.{uuid4()}", "filter", "BASELINE_CORRECTION") for _ in range(2)]
    plan = current_runtime.plan("processing", [decoder, *filters], settings={}, expected_revision=None)
    decoder["parameters"]["window_duration"] = 8
    mapping["outputs"].reverse()
    candidate = current_runtime.resolve_plan(plan.plan_id).candidates["processing"]
    assert current_runtime.profiles.repository.get_module("acquisition") == source
    assert candidate.bindings[0].parameters["window_duration"] != 8
    assert [row.name for row in candidate.bindings[0].processing_input.outputs] == ["Right", "Left"]
    operation = current_runtime.submit(plan.plan_id, str(uuid4()))
    await current_runtime.wait_for_idle()
    assert current_runtime.operation(operation["operation_id"])["status"] == "succeeded"
    updated = await _apply_current(current_runtime, [decoder, *reversed(filters)], "processing")
    assert current_runtime.profiles.repository.get_module("acquisition") == source
    assert [binding.slot for binding in updated.bindings if binding.kind == "filter"] == [
        item["slot"] for item in reversed(filters)
    ]
    assert [row.name for row in updated.bindings[0].processing_input.outputs] == ["Left", "Right"]
    assert current_runtime.profiles.repository.list_profiles() == []


@pytest.mark.parametrize("changes", [
    {"slot": "processing.unknown"},
    {"slot": "processing.filter.invalid"},
    {"slot": "processing.epoch_detector"},
    {"kind": "filter"},
    {"configuration_version": 999},
    {"processing_input": None},
    {"processing_input": {"schema_version": 1, "outputs": []}},
    {"processing_input": {"outputs": [{"name": "Missing source"}]}},
    {"processing_input": {"outputs": [
        {"name": "Repeated", "source_channel_index": 0},
        {"name": "Repeated", "source_channel_index": 1},
    ]}},
])
def test_invalid_current_binding_cannot_change_configuration(current_runtime, changes):
    decoder = _draft(current_runtime, "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA")
    with pytest.raises((ValueError, KeyError)):
        current_runtime.plan("processing", [{**decoder, **changes}], settings={}, expected_revision=None)
    assert all(item is None for item in current_runtime.profiles.selected_configuration().revisions().values())
    assert current_runtime.profiles.repository.list_profiles() == []


def test_current_decoder_requires_an_explicit_mapping(current_runtime):
    decoder = _draft(current_runtime, "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA")
    decoder.pop("processing_input")
    with pytest.raises(ValueError, match="processing_input"):
        current_runtime.plan("processing", [decoder], settings={}, expected_revision=None)
    assert all(item is None for item in current_runtime.profiles.selected_configuration().revisions().values())


def test_non_decoder_current_binding_rejects_processing_input(current_runtime):
    source = _draft(current_runtime, "acquisition.source", "acquisition", "file_reader",
                    processing_input={"schema_version": 2, "outputs": []})
    with pytest.raises(ValueError, match="processing_input"):
        current_runtime.plan("acquisition", [source], settings={}, expected_revision=None)


@pytest.fixture
def mod_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return ConfigurationService(
        TomlConfigurationRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
    )


def _mod_definition(profiles, kind, alias):
    return profiles.catalog.get(kind, builtin_selection(kind, alias))


def _mod_source_request(profiles):
    plugin = _mod_definition(profiles, "acquisition", "neuroscan_scan45_reader")
    return {"slot": "acquisition.source", "selection": plugin.selection.to_dict(),
            "parameters": {"DATA_PROVIDER_IP": "127.0.0.1", "DATA_PROVIDER_PORT": 4000,
                           "sample_rate": 1000, "channel_count": 65}}


def _stopped_runtime(profiles):
    from omnibci.application.services.engine_service import NeuroEngineService

    engine = NeuroEngineService(lambda _: pytest.fail("Selection must not start an engine"))
    return RuntimeConfigurationService(profiles, engine)


def _accept_plan(profiles, rt, plan):
    stored = rt.resolve_plan(plan.plan_id)
    if stored.engine:
        profiles.repository.save_engine(stored.engine, expected_revision=plan.expected_revisions["engine"])
    for module, candidate in stored.candidates.items():
        profiles.repository.save_module(candidate, expected_revision=plan.expected_revisions[module])


def test_acquisition_can_be_selected_and_started_without_other_modules(mod_profiles):
    from omnibci.application.configuration.accepted import accepted_runtime_settings
    from omnibci.application.processing_source import source_id_for

    rt = _stopped_runtime(mod_profiles)
    plan = rt.plan("acquisition", [_mod_source_request(mod_profiles)], expected_revision=None, settings={})
    assert not plan.errors
    assert plan.signal_summary is None
    assert set(plan.target_revisions) == {"acquisition", "engine"}
    _accept_plan(mod_profiles, rt, plan)
    snapshot = accepted_runtime_settings(profiles=mod_profiles)
    assert [item["slot"] for item in snapshot["PLUGINS.bindings"]] == ["acquisition.source"]
    assert source_id_for(snapshot) == "OmniBCI.neuroscan_scan45_reader/default@0.1.0"
    assert "ACQUISITION.signal_source" not in snapshot


@pytest.mark.asyncio
async def test_first_acquisition_apply_executes_without_preparing_an_experiment(mod_profiles):
    from unittest.mock import Mock
    from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator

    rt = _stopped_runtime(mod_profiles)
    acquisition, commands = Mock(), Mock()
    rt.executor = RuntimeApplyCoordinator(
        rt.engine, mod_profiles, acquisition,
        SimpleNamespace(is_active=False, process_alive=False), commands, repository=mod_profiles.repository,
    )
    plan = rt.plan("acquisition", [_mod_source_request(mod_profiles)], expected_revision=None, settings={})
    operation = rt.submit(plan.plan_id, str(uuid4()))
    await rt.wait_for_idle()
    assert rt.operation(operation["operation_id"])["status"] == "succeeded"
    assert [b.slot for b in mod_profiles.repository.get_module("acquisition").bindings] == ["acquisition.source"]
    assert not acquisition.mock_calls and not commands.mock_calls
    assert mod_profiles.repository.get_engine() is not None
    assert mod_profiles.repository.list_module_history("acquisition")[0].entry_id == operation["operation_id"]


@pytest.mark.parametrize("problem", ["missing_package", "invalid_parameters"])
def test_acquisition_does_not_inspect_or_modify_dormant_decoder(mod_profiles, monkeypatch, problem):
    from omnibci.application.configuration.accepted import accepted_runtime_settings
    from omnibci.application.configuration.models import ProfileBinding

    plugin = _mod_definition(mod_profiles, "decoder", "THUBCI_SSVEP_classic40CCA")
    decoder = ProfileBinding("processing.decoder", "decoder", None, plugin.selection,
                             {"window_duration": 1.0, "offset_time": 0.0}, processing_input=ProcessingInput())
    if problem == "missing_package":
        decoder = replace(decoder, selection=replace(decoder.selection, version="99.0.0"))
    else:
        decoder = replace(decoder, parameters={"window_duration": "invalid"})
    previous = mod_profiles.repository.save_module(ModuleActivation("processing", {}, (decoder,)))
    get = mod_profiles.catalog.get

    def only_acquisition(kind, selection):
        assert kind == "acquisition", "Acquisition must not resolve another module's plugin"
        return get(kind, selection)

    monkeypatch.setattr(mod_profiles.catalog, "get", only_acquisition)
    rt = _stopped_runtime(mod_profiles)
    plan = rt.plan("acquisition", [_mod_source_request(mod_profiles)], expected_revision=None, settings={})
    assert not plan.errors
    assert plan.changed_slots == ("acquisition.source",)
    _accept_plan(mod_profiles, rt, plan)
    assert mod_profiles.repository.get_module("processing") == previous
    snapshot = accepted_runtime_settings(profiles=mod_profiles)
    assert [item["slot"] for item in snapshot["PLUGINS.bindings"]] == ["acquisition.source"]
    assert snapshot["CONFIG.module_revisions"]["processing"] is None


@pytest.mark.parametrize("module,slot,kind,alias", [
    ("processing", "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("stimulation", "stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("actuation", "actuation.effector", "effector", "logger"),
])
def test_each_module_can_be_configured_first(mod_profiles, module, slot, kind, alias):
    plugin = _mod_definition(mod_profiles, kind, alias)
    profile = mod_profiles.create(kind, plugin.selection, alias, {})
    rt = _stopped_runtime(mod_profiles)
    plan = rt.plan(module, [{"slot": slot, "profile_id": profile.profile_id}], expected_revision=None, settings={})
    assert not plan.errors
    _accept_plan(mod_profiles, rt, plan)
    assert [b.slot for b in mod_profiles.repository.get_module(module).bindings] == [slot]
    assert all(value is None for key, value in mod_profiles.selected_configuration().modules.items() if key != module)


def test_module_request_cannot_change_another_modules_slots(mod_profiles):
    with pytest.raises(PluginSchemaError, match="module"):
        mod_profiles.propose("processing", [_mod_source_request(mod_profiles)], settings=mod_profiles.default_module_settings("processing"))


def test_acquisition_still_validates_its_own_parameters(mod_profiles):
    req = _mod_source_request(mod_profiles)
    req["parameters"]["DATA_PROVIDER_PORT"] = "invalid"
    with pytest.raises(PluginSchemaError):
        _stopped_runtime(mod_profiles).plan("acquisition", [req], expected_revision=None, settings={})


def test_existing_engine_keeps_single_module_target_scoped(mod_profiles):
    from omnibci.application.configuration.definitions import ENGINE_SETTING_DEFAULTS

    mod_profiles.repository.save_engine(mod_profiles.propose_engine(ENGINE_SETTING_DEFAULTS), expected_revision=None)
    rt = _stopped_runtime(mod_profiles)
    plan = rt.plan("stimulation", [], expected_revision=None, settings={})
    assert set(plan.target_revisions) == {"stimulation"}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    from omnibci.application.services.engine_service import NeuroEngineService
    from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator

    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter())
    engine = NeuroEngineService(lambda _: pytest.fail("Selecting configuration must not start workers"))
    executor = RuntimeApplyCoordinator(engine, profiles, None,
                                       SimpleNamespace(is_active=False, process_alive=False), None, repository=profiles.repository)
    rt = RuntimeConfigurationService(profiles, engine, executor=executor)
    definition = profiles.catalog.get("acquisition", builtin_selection("acquisition", "file_reader"))
    bindings = [{"slot": "acquisition.source", "selection": definition.selection.to_dict(),
                 "parameters": {"file_reader_chunk_size": 100}}]
    return SimpleNamespace(profiles=profiles, runtime=rt, bindings=bindings)


async def _execute_flow(flow, plan, operation_id=None):
    assert not plan.errors
    operation = flow.runtime.submit(plan.plan_id, operation_id or str(uuid4()))
    await flow.runtime.wait_for_idle()
    return flow.runtime.operation(operation["operation_id"])


def _source_plan(flow):
    current = flow.profiles.repository.get_module("acquisition")
    return flow.runtime.plan("acquisition", flow.bindings, settings={},
                             expected_revision=current.revision if current else None)


@pytest.mark.asyncio
async def test_first_selection_initializes_engine_and_never_creates_saved_profile(flow):
    plan = _source_plan(flow)
    assert plan.expected_revision is None
    assert set(plan.target_revisions) == {"acquisition", "engine"}
    operation = await _execute_flow(flow, plan)
    assert operation["status"] == "succeeded", operation
    status = flow.runtime.status()
    assert status["accepted"]["acquisition"] == plan.target_revisions["acquisition"]
    assert status["accepted"]["engine"] == plan.target_revisions["engine"]
    assert status["accepted"]["processing"] is None
    assert status["applied"] is None
    assert flow.profiles.repository.list_profiles() == []


@pytest.mark.asyncio
async def test_restore_records_a_b_a_without_deduplicating_equal_content(flow):
    first = await _execute_flow(flow, _source_plan(flow))
    flow.bindings[0]["parameters"]["file_reader_chunk_size"] = 200
    second = await _execute_flow(flow, _source_plan(flow))
    restore = flow.runtime.revert_plan("acquisition", first["operation_id"])
    third = await _execute_flow(flow, restore)
    assert third["status"] == "succeeded", third
    history = flow.profiles.repository.list_module_history("acquisition")
    assert [entry.entry_id for entry in history] == [third["operation_id"], second["operation_id"], first["operation_id"]]
    assert history[0].revision == history[2].revision != history[1].revision
    assert history[0].restored_from == first["operation_id"]
    assert history[0].activation.bindings[0].parameters["file_reader_chunk_size"] == 100
    assert flow.profiles.repository.get_module("acquisition").revision == history[0].revision


@pytest.mark.asyncio
async def test_retry_and_no_change_do_not_duplicate_history(flow):
    plan = _source_plan(flow)
    first = await _execute_flow(flow, plan)
    repeated = await _execute_flow(flow, plan, first["operation_id"])
    assert repeated == first
    unchanged = _source_plan(flow)
    assert unchanged.strategy == "no_change"
    assert (await _execute_flow(flow, unchanged))["status"] == "succeeded"
    assert len(flow.profiles.repository.list_module_history("acquisition")) == 1


@pytest.mark.asyncio
async def test_unrelated_module_write_does_not_expire_a_plan(flow):
    assert (await _execute_flow(flow, _source_plan(flow)))["status"] == "succeeded"
    flow.bindings[0]["parameters"]["file_reader_chunk_size"] = 200
    plan = _source_plan(flow)
    flow.profiles.repository.save_module(flow.profiles.propose("stimulation", [], settings=flow.profiles.default_module_settings("stimulation")), expected_revision=None)
    assert flow.runtime.resolve_plan(plan.plan_id).plan == plan
    assert (await _execute_flow(flow, plan))["status"] == "succeeded"
    assert flow.profiles.repository.get_module("stimulation") is not None
    assert flow.profiles.repository.list_module_history("stimulation") == []


@pytest.mark.asyncio
async def test_failed_first_selection_restores_missing_engine_without_history(flow, monkeypatch):
    plan = _source_plan(flow)

    def fail(*args, **kwargs):
        raise OSError("Cannot replace module file")

    monkeypatch.setattr(flow.profiles.repository, "save_module", fail)
    result = await _execute_flow(flow, plan)
    assert result["status"] == "failed"
    assert flow.profiles.repository.get_engine() is None
    assert flow.profiles.repository.get_module("acquisition") is None
    assert flow.profiles.repository.list_module_history("acquisition") == []


@pytest.mark.asyncio
async def test_history_write_failure_reports_applied_values_without_quarantining_runtime(flow, monkeypatch):
    plan = _source_plan(flow)

    def fail(*args, **kwargs):
        raise OSError("History storage unavailable")

    monkeypatch.setattr(flow.profiles.repository, "record_module_history", fail)
    operation = await _execute_flow(flow, plan)
    assert operation["status"] == "succeeded"
    assert operation["result"]["applied"] is True
    assert operation["result"]["history_saved"] is False
    assert "History storage unavailable" in operation["result"]["warning"]
    assert flow.profiles.repository.get_module("acquisition").revision == plan.target_revisions["acquisition"]
    assert flow.runtime.status()["recovery"] == {}
    assert flow.profiles.repository.list_module_history("acquisition") == []


@pytest.mark.asyncio
async def test_changed_engine_rejects_pending_module_plan(flow):
    await _execute_flow(flow, _source_plan(flow))
    plan = _source_plan(flow)
    previous = flow.profiles.repository.get_engine()
    changed = flow.profiles.propose_engine({**previous.settings, "debug": True})
    flow.profiles.repository.save_engine(changed, expected_revision=previous.revision)
    with pytest.raises(ProfileRevisionConflict):
        flow.runtime.resolve_plan(plan.plan_id)


@pytest.fixture
def current_action():
    from unittest.mock import Mock
    from omnibci.application.configuration.models import ActivationSet, ProfileBinding
    from omnibci.application.plugin_management.identity import PluginSelection

    module = ModuleActivation("acquisition", {}, (ProfileBinding(
        slot="acquisition.source", kind="acquisition", profile_id=None,
        selection=PluginSelection("test.source", "default", "1.0.0"),
        parameters={}, configuration_version=1,
    ),))
    engine_settings = EngineSettings({})
    profiles = Mock(spec=ConfigurationService)
    profiles.get_module.side_effect = lambda section: module if section == "acquisition" else None
    profiles.get_engine.return_value = engine_settings
    profiles.selected_configuration.side_effect = lambda modules: ActivationSet(
        {section: profiles.get_module(section) for section in modules}, engine_settings)
    profiles.propose.return_value = module
    engine = SimpleNamespace(
        admission=RuntimeAdmission(), is_engine_ready=True, generation="engine",
        applied_revisions={"acquisition": module.revision, "engine": engine_settings.revision},
        applied_snapshot={}, restart_blocked=False, runtime_package_snapshots=(),
        is_module_running=lambda _: False, is_peer_connected=lambda _: False,
    )
    executor = SimpleNamespace(
        active_kinds=lambda _: {"acquisition"},
        acquisition=SimpleNamespace(handle_get_status=lambda _: SimpleNamespace(connected=False, streaming=False)),
        execute=AsyncMock(return_value={"outcome": "no_change"}),
        execute_current_action=AsyncMock(return_value="connected"),
        prepare_processing=AsyncMock(),
    )
    svc = RuntimeConfigurationService(profiles, engine, executor=executor)
    configuration = {"expected_revision": module.revision, "settings": {}, "bindings": []}
    return svc, executor, configuration


@pytest.mark.asyncio
async def test_connect_keeps_ownership_after_apply_until_connection_is_confirmed(current_action):
    svc, executor, configuration = current_action
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(*args):
        assert svc.engine.admission.owns("acquisition")
        entered.set()
        await release.wait()
        return "connected"

    executor.execute_current_action.side_effect = connect
    operation_id = str(uuid4())
    task = asyncio.create_task(svc.execute_current("acquisition", "connect", operation_id=operation_id, configuration=configuration))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(ProfileRevisionConflict):
        svc.engine.admission.reserve_transition("acquisition")
    assert svc.operation(operation_id)["result"]["configuration_applied"] is True
    assert svc.operation(operation_id)["status"] == "applying"
    release.set()
    result = await task
    assert result["status"] == "succeeded"
    assert result["result"]["action"] == "connected"


@pytest.mark.asyncio
async def test_current_action_is_idempotent_and_survives_http_cancellation(current_action):
    from copy import deepcopy

    svc, executor, configuration = current_action
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(*args):
        entered.set()
        await release.wait()
        return "connected"

    executor.execute_current_action.side_effect = connect
    operation_id = str(uuid4())
    task = asyncio.create_task(svc.execute_current("acquisition", "connect", operation_id=operation_id, configuration=configuration))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProfileRevisionConflict):
        await svc.execute_current("acquisition", "connect", operation_id=operation_id,
                                  configuration={**configuration, "settings": {"changed": True}})
    retry = asyncio.create_task(svc.execute_current("acquisition", "connect", operation_id=operation_id, configuration=deepcopy(configuration)))
    release.set()
    assert (await retry)["status"] == "succeeded"
    executor.execute.assert_awaited_once()
    executor.execute_current_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_waits_for_cancelled_connection_request_to_finish(current_action):
    svc, executor, configuration = current_action
    entered, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def connect(*args):
        entered.set()
        await release.wait()
        order.append("connected")
        return "connected"

    async def stop():
        stopping.set()
        async with svc.engine.admission.stop_transition("acquisition"):
            order.append("stopped")

    executor.execute_current_action.side_effect = connect
    req = asyncio.create_task(svc.execute_current(
        "acquisition", "connect", operation_id=str(uuid4()), configuration=configuration))
    await asyncio.wait_for(entered.wait(), 1)
    req.cancel()
    with pytest.raises(asyncio.CancelledError):
        await req
    stop_task = asyncio.create_task(stop())
    try:
        await asyncio.wait_for(stopping.wait(), 1)
        assert not stop_task.done()
        assert order == []
    finally:
        release.set()
        await asyncio.wait_for(stop_task, 1)
        await svc.wait_for_idle()
    assert order == ["connected", "stopped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["acquisition", "engine"])
async def test_current_action_rejects_its_own_or_foundation_recovery(current_action, scope):
    svc, executor, configuration = current_action
    svc.engine.admission.quarantine("Unconfirmed ownership", scope)
    with pytest.raises(ProfileRevisionConflict, match="uncertain runtime module"):
        await svc.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=configuration)
    executor.execute.assert_not_awaited()
    executor.execute_current_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_connection_retains_applied_configuration_result(current_action):
    svc, executor, configuration = current_action
    executor.execute_current_action.side_effect = RuntimeError("Source refused connection")
    result = await svc.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=configuration)
    assert result["status"] == "failed"
    assert result["result"]["configuration_applied"] is True
    assert result["error"] == "Source refused connection"


@pytest.mark.asyncio
async def test_processing_recovery_does_not_block_acquisition_connection(current_action):
    svc, executor, configuration = current_action
    svc.engine.admission.quarantine("old input failed", "processing")
    result = await svc.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=configuration)
    assert result["status"] == "succeeded"
    assert svc.engine.admission.recovery_status == {"processing": "old input failed"}


def _failed_history(svc, revision):
    identifier = str(uuid4())
    operation = dict(operation_id=identifier, plan_id=str(uuid4()), target_revisions={"processing": revision},
                     module="processing", affected_modules=["processing"],
                     status="recovery_required", stage="starting", error="Startup outcome unknown",
                     rolled_back=False, result=None)
    svc._operations[identifier] = operation
    svc._active_operations["processing"] = identifier
    return operation


def test_recovery_flag_reflects_quarantine_even_when_engine_is_not_ready(runtime):
    from omnibci.presentation.schemas.runtime_config import RuntimeConfigurationResponse

    svc, bindings, snapshot = runtime
    svc.engine.is_engine_ready = False
    svc.engine.admission = gate = RuntimeAdmission()
    failed = _failed_history(svc, snapshot.modules["processing"].revision)
    gate.quarantine("Unknown startup outcome")
    status = RuntimeConfigurationResponse.model_validate(svc.status())
    assert status.running is False
    assert status.recovery == {"engine": "Unknown startup outcome"}
    assert status.operations["processing"].model_dump() == failed
    plan = svc.plan("processing", [item for item in bindings if item["slot"].startswith("processing.")], expected_revision=snapshot.modules["processing"].revision, settings=svc.profiles.default_module_settings("processing"))
    with pytest.raises(ProfileRevisionConflict, match="uncertain runtime"):
        svc.submit(plan.plan_id, str(uuid4()))


@pytest.mark.asyncio
async def test_confirmed_stop_clears_current_recovery_and_allows_apply_with_failed_history(runtime):
    from omnibci.application.commands.engine_commands import StopEngineCommand
    from omnibci.application.services.engine_service import NeuroEngineService
    from omnibci.presentation.schemas.runtime_config import RuntimeConfigurationResponse

    svc, bindings, snapshot = runtime
    gate = RuntimeAdmission()
    svc.engine = NeuroEngineService(lambda snapshot: None, admission=gate)
    failed = _failed_history(svc, snapshot.modules["processing"].revision)
    gate.quarantine("Unknown startup outcome")
    assert svc.status()["recovery"] == {"engine": "Unknown startup outcome"}
    await svc.engine.handle_stop_engine(StopEngineCommand())
    status = RuntimeConfigurationResponse.model_validate(svc.status())
    assert status.running is False
    assert status.recovery == {}
    assert status.operations["processing"].model_dump() == failed
    with gate.command():
        pass
    svc.executor = SimpleNamespace(supported_strategies={"select_on_start", "no_change"},
                                   execute=AsyncMock(return_value={"outcome": "selected"}))
    plan = svc.plan("processing", [item for item in bindings if item["slot"].startswith("processing.")], expected_revision=snapshot.modules["processing"].revision, settings=svc.profiles.default_module_settings("processing"))
    operation = svc.submit(plan.plan_id, str(uuid4()))
    await svc.wait_for_idle()
    assert svc.operation(operation["operation_id"])["status"] == "succeeded"
    assert svc.operation(failed["operation_id"]) == failed
