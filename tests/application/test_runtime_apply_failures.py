"""Failure boundaries use real profile storage and observable fake workers."""
from omnibci.application.activation_settings import section_settings
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.events import CommandResult, StreamDrainReceipt
from omnibci.application.activation_settings import merge_module_settings
from omnibci.application.plugin_profiles import ActivationSet, ModuleActivation, ProfileRevisionConflict
from tests.plugin_configuration_helpers import accept_profiles
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.queries.acquisition_queries import AcquisitionStatusResult
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.command_result_service import CommandTimeoutError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.shared.events import EventType


class Engine:
    def __init__(self, snapshot, log):
        self.admission = RuntimeAdmission()
        self.applied_snapshot = {**snapshot, "CONFIG.generation": "original-generation"}
        self.is_engine_ready = True
        self.restart_blocked = False
        self.owned_unready = False
        self.start_failure = None
        self.log = log
        self.starts = []
        self.modules = {"acquisition": True, "processing": True}

    def is_module_running(self, module):
        return self.is_engine_ready and self.modules.get(module, False)

    def acknowledge_configuration(self, snapshot, *, modules=None):
        self.applied_snapshot = (deepcopy(snapshot) if modules is None else
                                 merge_module_settings(self.applied_snapshot, snapshot, modules))

    async def start_module_for_configuration(self, module, snapshot):
        revision = snapshot["CONFIG.module_revisions"][module]
        self.log.append(("start_module", module, revision))
        self.starts.append(revision)
        if revision == self.start_failure:
            raise RuntimeError("candidate startup failed")
        self.modules[module] = True

    async def stop_module_for_configuration(self, module):
        self.log.append(("stop_module", module))
        self.modules[module] = False
        if module == "acquisition":
            self.acquisition.reset()

    @property
    def applied_revisions(self):
        return {**self.applied_snapshot["CONFIG.module_revisions"], "system": self.applied_snapshot["CONFIG.system_revision"]}

    def is_peer_connected(self, role):
        return False

    @property
    def generation(self):
        return self.applied_snapshot["CONFIG.generation"]

    @property
    def foundation_requires_cleanup(self):
        return self.is_engine_ready or self.owned_unready or self.restart_blocked

    @property
    def runtime_package_snapshots(self):
        return (deepcopy(self.applied_snapshot),) if self.is_engine_ready or self.owned_unready else ()

    @asynccontextmanager
    async def runtime_transition(self, scopes=None):
        with self.admission.transition(scopes):
            yield

    async def start_for_configuration(self, snapshot):
        revision = snapshot["CONFIG.module_revisions"]["acquisition"]
        self.log.append(("start_graph", revision))
        self.starts.append(revision)
        if revision == self.start_failure:
            self.is_engine_ready = False
            raise RuntimeError("candidate startup failed")
        self.applied_snapshot = {**deepcopy(snapshot), "CONFIG.generation": f"generation-{len(self.starts)}"}
        self.is_engine_ready = True

    async def stop_for_configuration(self):
        self.log.append(("stop_graph", self.applied_revisions["acquisition"]))
        self.is_engine_ready = False


class Acquisition:
    def __init__(self, engine, log):
        self.engine, self.log = engine, log
        self.connected = self.streaming = True
        self.cursor = 100
        self.receipt_override = {}
        self.unknown_start = False
        self.starts = []

    def handle_get_status(self, query):
        return AcquisitionStatusResult(self.connected, self.streaming)

    def handle_stop_and_drain(self):
        self.log.append(("drain", self.engine.generation))
        self.streaming = False
        receipt = StreamDrainReceipt(self.engine.generation, self.engine.applied_revisions["acquisition"],
                                     9, 1, True, processing_revision=self.engine.applied_revisions["processing"],
                                     system_revision=self.engine.applied_revisions["system"])
        return replace(receipt, **self.receipt_override)

    def reset(self):
        self.connected = self.streaming = False
        self.cursor = 0

    def handle_connect(self, command):
        self.connected = True

    def handle_start_stream(self, command):
        self.starts.append((self.engine.applied_revisions["acquisition"], self.cursor))
        self.log.append(("start_stream", self.engine.applied_revisions["acquisition"], self.cursor))
        self.streaming = True
        if self.unknown_start:
            raise CommandTimeoutError("start-unknown", "StartStream", 0.01)


class Stimulation:
    is_active = False
    process_alive = False

    def handle_start_stimulation(self, command, *, settings_snapshot=None):
        self.is_active = self.process_alive = True

    def handle_stop_stimulation(self, command):
        self.is_active = self.process_alive = False


class Commands:
    def __init__(self, engine, acquisition, log):
        self.engine, self.acquisition, self.log = engine, acquisition, log
        self.settle_failure = None
        self.restores = []

    def request(self, event_type, payload=None, **kwargs):
        revision = self.engine.applied_revisions["acquisition"]
        self.log.append((event_type.name, revision))
        identity = {"generation": self.engine.generation, "acquisition_revision": revision,
                    "system_revision": self.engine.applied_revisions["system"],
                    "module_revision": self.engine.applied_revisions[
                        "stimulation" if event_type is EventType.PauseStimulation else "actuation"]}
        if event_type is EventType.SettleActuation:
            if revision == self.settle_failure:
                raise RuntimeError("output completion could not be confirmed")
            data = {**identity, "paused": True, "execution_complete": True, "outputs_settled": True}
        elif event_type is EventType.GetSourceCheckpoint:
            data = {**identity, "supported": True, "checkpoint": {
                "source_identity": {"resource_id": "recording", "revision": "content-version"},
                "cursor": self.acquisition.cursor,
            }}
        elif event_type is EventType.RestoreSourceCheckpoint:
            assert payload["generation"] == self.engine.generation
            assert payload["expected_revision"] == revision
            self.acquisition.cursor = payload["checkpoint"]["cursor"]
            self.restores.append((revision, self.acquisition.cursor))
            data = {**identity, "restored": True}
        elif event_type is EventType.PauseStimulation:
            data = {**identity, "paused": True, "renderer_stopped": True}
        else:
            raise AssertionError(f"Unexpected worker request: {event_type}")
        return CommandResult("confirmed", event_type.name, True, data=data)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))

    def create(*, actuation=False):
        host = {"ACTUATION.auto_spawn": True,
                "STIMULATION.auto_spawn": True}
        profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "profiles"),
                                        CatalogConfigurationAdapter(), settings_source=lambda: host)
        previous = accept_profiles(profiles, [
            ("acquisition.source", "acquisition", "file_reader", {}),
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
            ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
            ("actuation.effector", "effector", "logger", {}),
        ])
        bindings = [{"slot": binding.slot, "profile_id": binding.profile_id}
                    for binding in previous.modules["acquisition"].bindings]
        log = []
        engine = Engine(profiles.materialize(previous), log)
        acquisition = Acquisition(engine, log)
        engine.acquisition = acquisition
        commands = Commands(engine, acquisition, log)
        engine.actuation_process_alive = actuation
        executor = RuntimeApplyCoordinator(engine, profiles, acquisition, Stimulation(), commands)
        runtime = RuntimeConfigurationService(profiles, engine, executor=executor)
        source = profiles.repository.get_profile(bindings[0]["profile_id"])
        profiles.update(source.profile_id, name=source.name,
                        parameters={**source.parameters, "file_reader_chunk_size": 50},
                        expected_revision=source.revision)
        plan = runtime.plan("acquisition", bindings, expected_revision=previous.modules["acquisition"].revision, settings=section_settings(runtime.profiles.settings_source(), "acquisition"))
        assert not plan.errors
        assert plan.strategy == "rebuild"
        return SimpleNamespace(engine=engine, profiles=profiles, previous=previous, plan=plan,
                               acquisition=acquisition, commands=commands,
                               runtime=runtime, log=log, module="acquisition")

    return create


async def apply(harness):
    operation = harness.runtime.submit(harness.plan.plan_id, str(uuid4()))
    await harness.runtime.wait_for_idle()
    return harness.runtime.operation(operation["operation_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["owned_unready", "restart_blocked"])
@pytest.mark.parametrize("before_review", [False, True])
async def test_idle_owned_resources_reject_review_or_resolve_before_submission(harness, ownership, before_review):
    h = harness()
    h.engine.is_engine_ready = False
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.modules[h.module].bindings]
    setattr(h.engine, ownership, before_review)
    plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision, settings=section_settings(h.runtime.profiles.settings_source(), h.module))
    if before_review:
        assert any("still owns resources" in error for error in plan.errors)
    else:
        assert not plan.errors
        setattr(h.engine, ownership, True)

    with pytest.raises(PluginSchemaError, match="still owns resources"):
        h.runtime.resolve_plan(plan.plan_id)
    with pytest.raises(PluginSchemaError, match="still owns resources"):
        h.runtime.submit(plan.plan_id, str(uuid4()))

    assert all(operation is None for operation in h.runtime.status()["operations"].values())
    assert h.profiles.repository.get_activation_set() == h.previous
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["owned_unready", "restart_blocked"])
async def test_idle_selection_rechecks_unreleased_resources_before_persistence(harness, ownership):
    h = harness()
    h.engine.is_engine_ready = False
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.modules[h.module].bindings]
    h.plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision, settings=section_settings(h.runtime.profiles.settings_source(), h.module))
    assert h.plan.strategy == "select_on_start" and not h.plan.errors
    stored = h.runtime.resolve_plan(h.plan.plan_id)
    setattr(h.engine, ownership, True)

    with pytest.raises(ProfileRevisionConflict, match="still owns resources"):
        await h.runtime.executor.execute(stored, lambda stage: None, operation_id=str(uuid4()))

    assert h.profiles.repository.get_activation_set() == h.previous
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_unready", [False, True])
async def test_idle_effector_validation_uses_owned_resources_not_history(harness, monkeypatch, owned_unready):
    h = harness(actuation=True)
    h.engine.is_engine_ready = False
    h.engine.owned_unready = owned_unready
    h.engine.actuation_process_alive = owned_unready
    h.module = "actuation"
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.modules[h.module].bindings]
    h.profiles.settings_source = lambda: {**h.previous.settings(), "ACTUATION.watchdog_timeout_s": 3.0}
    get = h.profiles.catalog.get

    def unavailable_effector(kind, selection):
        definition = get(kind, selection)
        return replace(definition, available=False) if kind == "effector" else definition

    monkeypatch.setattr(h.profiles.catalog, "get", unavailable_effector)
    h.plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision,
                            refresh_slots=[], settings=section_settings(h.runtime.profiles.settings_source(), h.module))
    candidate = h.runtime._plans[h.plan.plan_id].activation_set(h.previous)
    assert ("effector" in h.runtime.executor.active_kinds(candidate)) is owned_unready

    if owned_unready:
        assert any("still owns resources" in error for error in h.plan.errors)
    else:
        assert not h.plan.errors
        operation = await apply(h)
        assert operation["status"] == "succeeded"
        assert operation["result"] == {"outcome": "selected", "applied": True, "history_saved": True}
        assert h.profiles.repository.get_module(h.module).settings["ACTUATION.watchdog_timeout_s"] == 3.0
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_same_values_can_update_profile_reference_without_runtime_work(harness, running):
    h = harness()
    h.engine.is_engine_ready = running
    h.module = "processing"
    previous = next(binding for binding in h.previous.modules[h.module].bindings if binding.slot == "processing.decoder")
    clone = h.profiles.create("decoder", previous.selection, "Equivalent decoder profile", previous.parameters)
    bindings = [{"slot": binding.slot,
                 "profile_id": clone.profile_id if binding.slot == previous.slot else binding.profile_id}
                for binding in h.previous.modules[h.module].bindings]
    h.plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision,
                            refresh_slots=[previous.slot], settings=section_settings(h.runtime.profiles.settings_source(), h.module))
    assert h.plan.strategy == "no_change"
    assert h.plan.target_revisions[h.module] == h.previous.modules[h.module].revision

    operation = await apply(h)

    assert operation["status"] == "succeeded"
    assert operation["result"] == {"outcome": "no_change"}
    accepted = h.profiles.repository.get_module(h.module)
    assert accepted.revision == h.previous.modules[h.module].revision
    assert next(binding for binding in accepted.bindings if binding.slot == previous.slot).profile_id == clone.profile_id
    assert h.engine.applied_revisions[h.module] == h.previous.modules[h.module].revision
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["acquisition", "processing"])
async def test_stopped_module_configuration_is_selected_without_starting_it(harness, module):
    h = harness()
    h.module = module
    h.engine.modules[module] = False
    previous = h.previous.modules[module]
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in previous.bindings]
    if module == "processing":
        decoder = h.profiles.repository.get_profile(bindings[0]["profile_id"])
        h.profiles.update(decoder.profile_id, name=decoder.name,
                          parameters={**decoder.parameters, "window_duration": 2.5},
                          expected_revision=decoder.revision)
    h.plan = h.runtime.plan(module, bindings, expected_revision=previous.revision, settings=section_settings(h.runtime.profiles.settings_source(), module))
    assert h.plan.strategy == "select_on_start" and not h.plan.errors
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert operation["result"] == {"outcome": "selected", "applied": True, "history_saved": True}
    assert not h.engine.is_module_running(module)
    assert h.engine.is_engine_ready
    assert h.engine.applied_revisions[module] == h.profiles.repository.get_module(module).revision
    assert h.log == []


@pytest.mark.asyncio
@pytest.mark.parametrize("restoration_fails", [False, True])
async def test_inactive_selection_acknowledgement_failure_restores_or_quarantines(harness, restoration_fails):
    h = harness()
    h.engine.modules["acquisition"] = False
    bindings = [{"slot": item.slot, "profile_id": item.profile_id}
                for item in h.previous.modules["acquisition"].bindings]
    h.plan = h.runtime.plan("acquisition", bindings, expected_revision=h.previous.modules["acquisition"].revision, settings=section_settings(h.runtime.profiles.settings_source(), "acquisition"))
    assert h.plan.strategy == "select_on_start"
    before = deepcopy(h.engine.applied_snapshot)

    def acknowledge(snapshot, *, modules=None):
        h.engine.applied_snapshot = merge_module_settings(h.engine.applied_snapshot, snapshot, modules)
        if restoration_fails or snapshot["CONFIG.module_revisions"]["acquisition"] == h.plan.target_revisions["acquisition"]:
            raise RuntimeError("Runtime configuration acknowledgement failed")

    h.engine.acknowledge_configuration = acknowledge
    operation = await apply(h)
    assert operation["status"] == ("recovery_required" if restoration_fails else "failed")
    assert operation["rolled_back"] is not restoration_fails
    assert h.profiles.repository.get_activation_set() == h.previous
    assert h.profiles.repository.list_module_history("acquisition") == []
    assert not h.engine.is_module_running("acquisition")
    assert h.log == []
    if restoration_fails:
        assert_quarantined(h)
    else:
        assert h.engine.applied_snapshot == before
        assert not h.engine.admission.recovery_required


def assert_quarantined(harness):
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        with harness.engine.admission.command():
            pytest.fail("Ordinary commands were admitted after an uncertain outcome")


@pytest.mark.asyncio
async def test_acquisition_start_failure_rolls_back_without_recreating_active_outputs(harness):
    h = harness(actuation=True)
    h.engine.start_failure = h.plan.target_revisions[h.module]
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revisions[h.module], h.previous.modules[h.module].revision]
    assert h.acquisition.starts == [(h.previous.modules[h.module].revision, 100)]
    assert h.profiles.repository.get_module(h.module).revision == h.previous.modules[h.module].revision
    assert not any(item[0] in {"SettleActuation", "PauseStimulation", "stop_graph"} for item in h.log)
    assert not h.engine.admission.recovery_required


@pytest.mark.asyncio
async def test_system_output_settlement_failure_keeps_old_graph_paused(harness):
    h = harness(actuation=True)
    h.commands.settle_failure = h.previous.modules[h.module].revision
    h.profiles.settings_source = lambda: {**h.previous.settings(), "debug": True}
    h.plan = h.runtime.plan("system", [], expected_revision=h.previous.system.revision, settings=section_settings(h.profiles.settings_source(), "system"))
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.engine.starts == []
    assert not h.acquisition.streaming
    assert not any(item[0] == "stop_graph" for item in h.log)
    assert_quarantined(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"generation": "wrong-generation"}, {"acquisition_revision": "wrong-revision"}, {"processing_revision": "wrong-revision"}, {"system_revision": "wrong-system"},
])
async def test_unconfirmed_drain_never_rebuilds_the_graph(harness, invalid):
    h = harness()
    h.acquisition.receipt_override = invalid
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.engine.starts == []
    assert h.commands.restores == []
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_candidate_constructor_failure_without_actuation_rolls_back_checkpoint(harness):
    h = harness()
    h.engine.start_failure = h.plan.target_revisions[h.module]
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revisions[h.module], h.previous.modules[h.module].revision]
    assert h.acquisition.starts == [(h.previous.modules[h.module].revision, 100)]
    assert h.commands.restores == [(h.previous.modules[h.module].revision, 100)]
    with h.engine.admission.command():
        pass


@pytest.mark.asyncio
async def test_save_failure_restores_old_inputs_only_when_still_selected(harness, monkeypatch):
    h = harness()
    original_save = h.profiles.repository.save_module

    def fail_candidate(snapshot, *, expected_revision):
        if snapshot.revision == h.plan.target_revisions[h.module]:
            raise OSError("activation write failed before replacement")
        return original_save(snapshot, expected_revision=expected_revision)

    monkeypatch.setattr(h.profiles.repository, "save_module", fail_candidate)
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert operation["rolled_back"]
    assert h.engine.applied_revisions[h.module] == h.profiles.repository.get_module(h.module).revision == h.previous.modules[h.module].revision
    assert h.acquisition.starts == [(h.previous.modules[h.module].revision, 100)]
    assert h.commands.restores == [(h.plan.target_revisions[h.module], 100), (h.previous.modules[h.module].revision, 100)]


@pytest.mark.asyncio
async def test_activation_cas_conflict_never_resumes_before_reconciling_external_choice(harness, monkeypatch):
    h = harness()
    original_save = h.profiles.repository.save_module
    binding = h.previous.modules[h.module].bindings[0]
    external = ModuleActivation(h.module, h.previous.modules[h.module].settings,
                                (replace(binding, parameters={**binding.parameters, "file_reader_chunk_size": 999}),))

    def conflict(snapshot, *, expected_revision):
        original_save(external, expected_revision=h.previous.modules[h.module].revision)
        raise ProfileRevisionConflict("Another writer selected a configuration")

    monkeypatch.setattr(h.profiles.repository, "save_module", conflict)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.profiles.repository.get_module(h.module).revision == external.revision
    assert h.engine.starts == [h.plan.target_revisions[h.module]]
    assert h.acquisition.starts == []
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_unknown_stream_start_never_rolls_back_or_rewinds(harness):
    h = harness()
    h.acquisition.unknown_start = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revisions[h.module]]
    assert h.commands.restores == [(h.plan.target_revisions[h.module], 100)]
    assert h.acquisition.starts == [(h.plan.target_revisions[h.module], 100)]
    assert h.profiles.repository.get_module(h.module).revision == h.plan.target_revisions[h.module]
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_acquisition_persistence_failure_rolls_back_without_settling_outputs(harness, monkeypatch):
    h = harness(actuation=True)
    h.commands.settle_failure = h.plan.target_revisions[h.module]

    def fail_save(*args, **kwargs):
        raise OSError("activation write failed")

    monkeypatch.setattr(h.profiles.repository, "save_module", fail_save)
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revisions[h.module], h.previous.modules[h.module].revision]
    assert h.acquisition.starts == [(h.previous.modules[h.module].revision, 100)]
    assert not any(item[0] in {"SettleActuation", "PauseStimulation", "stop_graph"} for item in h.log)
    assert not h.engine.admission.recovery_required
