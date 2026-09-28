"""Failure boundaries use real profile/session storage and observable fake workers."""
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand
from omnibci.application.events import CommandResult, StreamDrainReceipt, TrialOutcome
from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.queries.acquisition_queries import AcquisitionStatusResult
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.command_result_service import CommandTimeoutError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.classification import Classification
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.shared.events import Event, EventType


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

    @property
    def applied_revision(self):
        return self.applied_snapshot["CONFIG.revision"]

    @property
    def generation(self):
        return self.applied_snapshot["CONFIG.generation"]

    @property
    def runtime_package_snapshots(self):
        return (deepcopy(self.applied_snapshot),) if self.is_engine_ready or self.owned_unready else ()

    @asynccontextmanager
    async def runtime_transition(self):
        with self.admission.transition():
            yield

    async def start_for_configuration(self, snapshot):
        revision = snapshot["CONFIG.revision"]
        self.log.append(("start_graph", revision))
        self.starts.append(revision)
        if revision == self.start_failure:
            self.is_engine_ready = False
            raise RuntimeError("candidate startup failed")
        self.applied_snapshot = {**deepcopy(snapshot), "CONFIG.generation": f"generation-{len(self.starts)}"}
        self.is_engine_ready = True

    async def stop_for_configuration(self):
        self.log.append(("stop_graph", self.applied_revision))
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

    def handle_stop_and_drain(self, *, session_id):
        self.log.append(("drain", session_id))
        self.streaming = False
        receipt = StreamDrainReceipt(self.engine.generation, self.engine.applied_revision,
                                     9, session_id, 1, True)
        return replace(receipt, **self.receipt_override)

    def reset(self):
        self.connected = self.streaming = False
        self.cursor = 0

    def handle_connect(self, command):
        self.connected = True

    def handle_start_stream(self, command):
        self.starts.append((self.engine.applied_revision, self.cursor))
        self.log.append(("start_stream", self.engine.applied_revision, self.cursor))
        self.streaming = True
        if self.unknown_start:
            raise CommandTimeoutError("start-unknown", "StartStream", 0.01)


class Stimulation:
    is_active = False
    process_alive = False

    def handle_start_stimulation(self, command):
        self.is_active = self.process_alive = True

    def handle_stop_stimulation(self, command):
        self.is_active = self.process_alive = False


class Commands:
    def __init__(self, engine, acquisition, log):
        self.engine, self.acquisition, self.log = engine, acquisition, log
        self.settle_failure = None
        self.restores = []

    def request(self, event_type, payload=None, **kwargs):
        revision = self.engine.applied_revision
        self.log.append((event_type.name, revision))
        identity = {"generation": self.engine.generation, "config_revision": revision}
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
        host = {"ACTUATION.enabled": actuation, "ACTUATION.auto_spawn": True,
                "STIMULATION.auto_spawn": True}
        profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "profiles"),
                                        CatalogConfigurationAdapter(), settings_source=lambda: host)
        bindings = []
        for slot, kind, alias in (
            ("acquisition.source", "acquisition", "file_reader"),
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
            ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
            ("actuation.effector", "effector", "logger"),
        ):
            profile = profiles.create(kind, profiles.catalog.legacy(kind, alias).selection, alias, {})
            bindings.append({"slot": slot, "profile_id": profile.profile_id})
        previous = profiles.propose(bindings)
        profiles.accept(previous, expected_revision=None)
        log = []
        engine = Engine(profiles.materialize(previous), log)
        acquisition = Acquisition(engine, log)
        commands = Commands(engine, acquisition, log)
        repository = InMemorySessionRepository()
        sessions = SessionAppService(repository, admission=engine.admission)
        original_session = sessions.handle_create_session(CreateSessionCommand(config_revision=previous.revision))
        sessions.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome(
            original_session, 1, Classification(target_id=1, event_id=1), previous.revision,
        )))
        executor = RuntimeApplyCoordinator(engine, profiles, acquisition, sessions, Stimulation(), commands)
        runtime = RuntimeConfigurationService(profiles, engine, executor=executor)
        source = profiles.repository.get_profile(bindings[0]["profile_id"])
        profiles.update(source.profile_id, name=source.name,
                        parameters={**source.parameters, "file_reader_chunk_size": 50},
                        expected_revision=source.revision)
        plan = runtime.plan(bindings, expected_revision=previous.revision)
        assert not plan.errors
        assert plan.strategy == "rebuild"
        return SimpleNamespace(engine=engine, profiles=profiles, previous=previous, plan=plan,
                               acquisition=acquisition, commands=commands, sessions=sessions,
                               repository=repository, original_session=original_session,
                               runtime=runtime, log=log)

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
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.bindings]
    setattr(h.engine, ownership, before_review)
    plan = h.runtime.plan(bindings, expected_revision=h.previous.revision)
    if before_review:
        assert any("still owns resources" in error for error in plan.errors)
    else:
        assert not plan.errors
        setattr(h.engine, ownership, True)

    with pytest.raises(PluginSchemaError, match="still owns resources"):
        h.runtime.resolve_plan(plan.plan_id)
    with pytest.raises(PluginSchemaError, match="still owns resources"):
        h.runtime.submit(plan.plan_id, str(uuid4()))

    assert h.runtime.status()["operation"] is None
    assert h.profiles.repository.get_activation() == h.previous
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["owned_unready", "restart_blocked"])
async def test_idle_selection_rechecks_unreleased_resources_before_persistence(harness, ownership):
    h = harness()
    h.engine.is_engine_ready = False
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.bindings]
    h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision)
    assert h.plan.strategy == "select_on_start" and not h.plan.errors
    stored = h.runtime.resolve_plan(h.plan.plan_id)
    setattr(h.engine, ownership, True)

    with pytest.raises(ProfileRevisionConflict, match="still owns resources"):
        await h.runtime.executor.execute(stored, lambda stage: None)

    assert h.profiles.repository.get_activation() == h.previous
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_unready", [False, True])
async def test_disabled_idle_effector_validation_uses_owned_resources_not_history(harness, monkeypatch, owned_unready):
    h = harness(actuation=True)
    h.engine.is_engine_ready = False
    h.engine.owned_unready = owned_unready
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.bindings]
    h.profiles.settings_source = lambda: {**h.previous.settings, "ACTUATION.enabled": False}
    get = h.profiles.catalog.get

    def unavailable_effector(kind, selection):
        definition = get(kind, selection)
        return replace(definition, available=False) if kind == "effector" else definition

    monkeypatch.setattr(h.profiles.catalog, "get", unavailable_effector)
    h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision,
                            refresh_slots=[], include_host_settings=True)
    candidate = h.runtime._plans[h.plan.plan_id].snapshot
    assert ("effector" in h.runtime.executor.active_kinds(candidate)) is owned_unready

    if owned_unready:
        assert any("still owns resources" in error for error in h.plan.errors)
    else:
        assert not h.plan.errors
        operation = await apply(h)
        assert operation["status"] == "succeeded"
        assert operation["result"] == {"outcome": "selected"}
        assert h.profiles.repository.get_activation().settings["ACTUATION.enabled"] is False
    assert h.log == []
    assert h.engine.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_same_values_can_update_profile_reference_without_runtime_work(harness, running):
    h = harness()
    h.engine.is_engine_ready = running
    previous = next(binding for binding in h.previous.bindings if binding.slot == "processing.decoder")
    clone = h.profiles.create("decoder", previous.selection, "Equivalent decoder profile", previous.parameters)
    bindings = [{"slot": binding.slot,
                 "profile_id": clone.profile_id if binding.slot == previous.slot else binding.profile_id}
                for binding in h.previous.bindings]
    h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision,
                            refresh_slots=[previous.slot])
    assert h.plan.strategy == ("no_change" if running else "select_on_start")
    assert h.plan.target_revision == h.previous.revision

    operation = await apply(h)

    assert operation["status"] == "succeeded"
    assert operation["result"] == {"outcome": "applied" if running else "selected"}
    accepted = h.profiles.repository.get_activation()
    assert accepted.revision == h.previous.revision
    assert next(binding for binding in accepted.bindings if binding.slot == previous.slot).profile_id == clone.profile_id
    assert h.engine.applied_revision == h.previous.revision
    assert h.log == []
    assert h.engine.starts == []


def assert_quarantined(harness):
    with pytest.raises(ProfileRevisionConflict, match="recovery"):
        with harness.engine.admission.command():
            pytest.fail("Ordinary commands were admitted after an uncertain outcome")


@pytest.mark.asyncio
async def test_candidate_actuator_startup_unknown_does_not_recreate_outputs(harness):
    h = harness(actuation=True)
    h.engine.start_failure = h.plan.target_revision
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revision]
    assert h.acquisition.starts == []
    assert h.profiles.repository.get_activation().revision == h.previous.revision
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_output_settlement_failure_keeps_old_session_open_and_paused(harness):
    h = harness(actuation=True)
    h.commands.settle_failure = h.previous.revision
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.engine.starts == []
    assert h.sessions.active_session_id == h.original_session
    assert not h.acquisition.streaming
    assert not any(item[0] == "stop_graph" for item in h.log)
    assert_quarantined(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"generation": "wrong-generation"}, {"config_revision": "wrong-revision"},
    {"completed_trials": 2},
])
async def test_unconfirmed_drain_or_missing_projection_never_closes_session(harness, invalid):
    h = harness()
    h.acquisition.receipt_override = invalid
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.sessions.active_session_id == h.original_session
    assert h.repository.find_by_id(h.original_session).state == "running"
    assert h.engine.starts == []
    assert h.commands.restores == []
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_candidate_constructor_failure_without_actuation_rolls_back_checkpoint(harness):
    h = harness()
    h.engine.start_failure = h.plan.target_revision
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revision, h.previous.revision]
    assert h.acquisition.starts == [(h.previous.revision, 100)]
    assert h.commands.restores == [(h.previous.revision, 100)]
    assert h.sessions.active_session_id != h.original_session
    assert h.repository.find_by_id(h.original_session).state == "ended"
    assert h.repository.find_by_id(h.sessions.active_session_id).continuation_of == h.original_session
    with h.engine.admission.command():
        pass


@pytest.mark.asyncio
async def test_save_failure_restores_old_inputs_only_when_still_selected(harness, monkeypatch):
    h = harness()
    original_save = h.profiles.repository.save_activation

    def fail_candidate(snapshot, *, expected_revision):
        if snapshot.revision == h.plan.target_revision:
            raise OSError("activation write failed before replacement")
        return original_save(snapshot, expected_revision=expected_revision)

    monkeypatch.setattr(h.profiles.repository, "save_activation", fail_candidate)
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert operation["rolled_back"]
    assert h.engine.applied_revision == h.profiles.repository.get_activation().revision == h.previous.revision
    assert h.acquisition.starts == [(h.previous.revision, 100)]
    assert h.commands.restores == [(h.plan.target_revision, 100), (h.previous.revision, 100)]
    sessions = h.repository.find_all()
    assert len({item.session_id for item in sessions}) == 3
    assert [item.state for item in sessions] == ["ended", "ended", "running"]


@pytest.mark.asyncio
async def test_activation_cas_conflict_never_resumes_before_reconciling_external_choice(harness, monkeypatch):
    h = harness()
    original_save = h.profiles.repository.save_activation
    external = ActivationSnapshot({**h.previous.settings, "EXTERNAL.choice": "changed"}, h.previous.bindings)

    def conflict(snapshot, *, expected_revision):
        original_save(external, expected_revision=h.previous.revision)
        raise ProfileRevisionConflict("Another writer selected a configuration")

    monkeypatch.setattr(h.profiles.repository, "save_activation", conflict)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.profiles.repository.get_activation().revision == external.revision
    assert h.engine.starts == [h.plan.target_revision]
    assert h.acquisition.starts == []
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_unknown_stream_start_never_rolls_back_or_rewinds(harness):
    h = harness()
    h.acquisition.unknown_start = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revision]
    assert h.commands.restores == [(h.plan.target_revision, 100)]
    assert h.acquisition.starts == [(h.plan.target_revision, 100)]
    assert h.profiles.repository.get_activation().revision == h.plan.target_revision
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_candidate_output_settlement_failure_blocks_rollback(harness, monkeypatch):
    h = harness(actuation=True)
    h.commands.settle_failure = h.plan.target_revision

    def fail_save(*args, **kwargs):
        raise OSError("activation write failed")

    monkeypatch.setattr(h.profiles.repository, "save_activation", fail_save)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.engine.starts == [h.plan.target_revision]
    assert h.acquisition.starts == []
    assert [item for item in h.log if item[0] == "SettleActuation"] == [
        ("SettleActuation", h.previous.revision), ("SettleActuation", h.plan.target_revision),
    ]
    assert_quarantined(h)
