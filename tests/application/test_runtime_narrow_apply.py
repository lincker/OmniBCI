"""Narrow updates preserve workers/session and reconcile uncertain receipts."""
from copy import deepcopy
from dataclasses import replace

import pytest

from omnibci.application.events import CommandResult
from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.command_result_service import CommandTimeoutError
from omnibci.shared.events import EventType
from tests.application.test_runtime_apply_failures import (
    Commands, apply, assert_quarantined, harness as rebuild_harness,
)


class NarrowCommands(Commands):
    def __init__(self, engine, acquisition, log):
        super().__init__(engine, acquisition, log)
        self.processing_revision = self.acquisition_revision = engine.applied_revision
        self.stage_outcome = "applied"
        self.cancel_outcome = "cancelled"
        self.bad_context_receipt = self.bad_source_receipt = False
        self.stage_targets = []
        self.context_targets = []

    def request(self, event_type, payload=None, **kwargs):
        identity = {"generation": self.engine.generation}
        if event_type is EventType.StageProcessingConfiguration:
            assert payload["expected_revision"] == self.processing_revision
            target = payload["settings_snapshot"]["CONFIG.revision"]
            self.stage_targets.append(target)
            if self.stage_outcome != "timeout_cancelled":
                self.processing_revision = target
            if self.stage_outcome.startswith("timeout_"):
                raise CommandTimeoutError("staged-command", event_type.name, .01)
            data = {**identity, "status": "applied", "applied_revision": target}
            if self.stage_outcome == "bad_receipt":
                data["generation"] = "stale-generation"
        elif event_type is EventType.CancelProcessingConfiguration:
            assert payload["command_id"] == "staged-command"
            if self.cancel_outcome == "unknown":
                raise CommandTimeoutError("cancel-command", event_type.name, .01)
            data = {**identity, "status": self.cancel_outcome,
                    "applied_revision": self.processing_revision,
                    "command_id": "staged-command"}
            if self.cancel_outcome == "wrong_command":
                data.update(status="applied", command_id="another-stage")
        elif event_type is EventType.UpdateProcessingContext:
            assert payload["expected_revision"] == self.processing_revision
            self.processing_revision = payload["settings_snapshot"]["CONFIG.revision"]
            self.context_targets.append(self.processing_revision)
            data = {**identity, "status": "applied", "applied_revision": self.processing_revision}
            if self.bad_context_receipt:
                data["applied_revision"] = "unexpected-revision"
        elif event_type is EventType.UpdateAcquisitionRevision:
            assert payload["expected_revision"] == self.acquisition_revision
            self.acquisition_revision = payload["target_revision"]
            data = {**identity, "config_revision": self.acquisition_revision}
            if self.bad_source_receipt:
                data["generation"] = "stale-generation"
        else:
            return super().request(event_type, payload, **kwargs)
        self.log.append((event_type.name, payload))
        return CommandResult("confirmed", event_type.name, True, data=data)


@pytest.fixture
def narrow(rebuild_harness):
    def create(strategy):
        h = rebuild_harness()
        h.commands = NarrowCommands(h.engine, h.acquisition, h.log)
        h.runtime.executor.commands = h.commands
        h.output_restarts = []

        def acknowledge(snapshot):
            h.engine.applied_snapshot = deepcopy(snapshot)

        async def restart_outputs(snapshot):
            h.output_restarts.append(snapshot["CONFIG.revision"])

        h.engine.acknowledge_configuration = acknowledge
        h.engine.restart_actuation_for_configuration = restart_outputs
        bindings = [{"slot": item.slot, "profile_id": item.profile_id} for item in h.previous.bindings]
        if strategy == "trial_boundary":
            decoder = h.profiles.repository.get_profile(bindings[1]["profile_id"])
            h.profiles.update(decoder.profile_id, name=decoder.name,
                              parameters={**decoder.parameters, "window_duration": 2.5},
                              expected_revision=decoder.revision)
            refresh = ["processing.decoder"]
        else:
            # Remove a dormant output selection. The enabled-output path uses
            # the same grouped restart boundary and is covered by real IPC tests.
            bindings = [item for item in bindings if item["slot"] != "actuation.effector"]
            refresh = []
        h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision, refresh_slots=refresh)
        assert not h.plan.errors
        assert h.plan.strategy == strategy
        return h
    return create


def assert_preserved(h):
    assert h.engine.starts == []
    assert h.engine.generation == "original-generation"
    assert h.sessions.active_session_id == h.original_session
    assert len(h.repository.find_all()) == 1
    assert h.repository.find_by_id(h.original_session).state == "running"


@pytest.mark.asyncio
async def test_trial_update_changes_revisions_without_pausing_or_replacing_workers(narrow):
    h = narrow("trial_boundary")
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.processing_revision == h.commands.acquisition_revision == h.plan.target_revision
    assert h.engine.applied_revision == h.profiles.repository.get_activation().revision == h.plan.target_revision
    assert h.output_restarts == []
    assert h.commands.restores == h.acquisition.starts == []
    assert not any(item[0] == "drain" for item in h.log)
    assert_preserved(h)


@pytest.mark.asyncio
async def test_trial_timeout_cancelled_retains_old_pipeline_without_quarantine(narrow):
    h = narrow("trial_boundary")
    h.commands.stage_outcome = "timeout_cancelled"
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert not operation["rolled_back"]
    assert h.commands.processing_revision == h.commands.acquisition_revision == h.previous.revision
    assert h.engine.applied_revision == h.profiles.repository.get_activation().revision == h.previous.revision
    with h.engine.admission.command():
        pass
    assert_preserved(h)


@pytest.mark.asyncio
async def test_trial_timeout_reconciled_applied_completes_without_second_stage(narrow):
    h = narrow("trial_boundary")
    h.commands.stage_outcome = "timeout_applied"
    h.commands.cancel_outcome = "applied"
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.stage_targets == [h.plan.target_revision]
    assert h.commands.processing_revision == h.commands.acquisition_revision == h.plan.target_revision
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_outcome", ["unknown", "wrong_command"])
async def test_trial_timeout_with_unconfirmed_cancel_quarantines(narrow, cancel_outcome):
    h = narrow("trial_boundary")
    h.commands.stage_outcome = "timeout_applied"
    h.commands.cancel_outcome = cancel_outcome
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.commands.stage_targets == [h.plan.target_revision]
    assert h.commands.acquisition_revision == h.previous.revision
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stage", "source"])
async def test_trial_receipt_mismatch_does_not_guess_rollback(narrow, fault):
    h = narrow("trial_boundary")
    if fault == "stage":
        h.commands.stage_outcome = "bad_receipt"
    else:
        h.commands.bad_source_receipt = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.commands.processing_revision == h.plan.target_revision
    assert h.commands.stage_targets == [h.plan.target_revision]
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("external_change", [False, True])
async def test_trial_persistence_failure_checks_selection_before_rollback(narrow, monkeypatch, external_change):
    h = narrow("trial_boundary")
    save = h.profiles.repository.save_activation
    external = ActivationSnapshot({**h.previous.settings, "EXTERNAL.choice": "other"}, h.previous.bindings)

    def fail_save(snapshot, *, expected_revision):
        if external_change:
            save(external, expected_revision=expected_revision)
            raise ProfileRevisionConflict("Another writer selected a configuration")
        raise OSError("save failed before replacement")

    monkeypatch.setattr(h.profiles.repository, "save_activation", fail_save)
    operation = await apply(h)
    if external_change:
        assert operation["status"] == "recovery_required"
        assert h.commands.stage_targets == [h.plan.target_revision]
        assert h.commands.processing_revision == h.plan.target_revision
        assert h.profiles.repository.get_activation().revision == external.revision
        assert_quarantined(h)
    else:
        assert operation["status"] == "failed" and operation["rolled_back"]
        assert h.commands.stage_targets == [h.plan.target_revision, h.previous.revision]
        assert h.commands.processing_revision == h.commands.acquisition_revision == h.previous.revision
        assert h.engine.applied_revision == h.previous.revision
    assert_preserved(h)


@pytest.mark.asyncio
async def test_module_update_drains_restores_cursor_and_preserves_session(narrow):
    h = narrow("module")
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert operation["result"]["session_preserved"]
    assert h.output_restarts == [h.plan.target_revision]
    assert h.commands.context_targets == [h.plan.target_revision]
    assert h.commands.restores == h.acquisition.starts == [(h.plan.target_revision, 100)]
    assert h.commands.stage_targets == []
    names = [item[0] for item in h.log]
    assert names.index("drain") < names.index("UpdateProcessingContext") < names.index("RestoreSourceCheckpoint")
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["context", "source"])
async def test_module_receipt_mismatch_cannot_acknowledge_a_guessed_old_revision(narrow, fault):
    h = narrow("module")
    h.commands.bad_context_receipt = fault == "context"
    h.commands.bad_source_receipt = fault == "source"
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.output_restarts == [h.plan.target_revision]
    assert h.commands.processing_revision == h.plan.target_revision
    assert h.commands.restores == h.acquisition.starts == []
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
async def test_module_save_failure_restores_old_context_and_cursor_without_new_session(narrow, monkeypatch):
    h = narrow("module")

    def fail_save(*args, **kwargs):
        raise OSError("save failed")

    monkeypatch.setattr(h.profiles.repository, "save_activation", fail_save)
    operation = await apply(h)
    assert operation["status"] == "failed" and operation["rolled_back"]
    assert h.output_restarts == [h.plan.target_revision, h.previous.revision]
    assert h.commands.context_targets == [h.plan.target_revision, h.previous.revision]
    assert h.commands.processing_revision == h.commands.acquisition_revision == h.previous.revision
    assert h.commands.restores == h.acquisition.starts == [(h.previous.revision, 100)]
    assert_preserved(h)


@pytest.mark.asyncio
async def test_module_unknown_resume_never_recreates_outputs_or_rewinds(narrow):
    h = narrow("module")
    h.acquisition.unknown_start = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.output_restarts == [h.plan.target_revision]
    assert h.commands.restores == h.acquisition.starts == [(h.plan.target_revision, 100)]
    assert h.engine.applied_revision == h.profiles.repository.get_activation().revision == h.plan.target_revision
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["module", "rebuild"])
@pytest.mark.parametrize("checkpoint_data", [
    {"supported": False, "checkpoint": None},
    {"supported": False, "checkpoint": None, "resume_without_checkpoint_safe": "true"},
    {"supported": True, "checkpoint": None},
    {"supported": True, "checkpoint": None, "resume_without_checkpoint_safe": True},
    {"supported": "true", "checkpoint": {}},
])
async def test_streaming_source_without_confirmed_checkpoint_is_not_automatically_restarted(
    narrow, rebuild_harness, monkeypatch, strategy, checkpoint_data,
):
    h = narrow("module") if strategy == "module" else rebuild_harness()
    request = h.commands.request

    def source_receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "config_revision": h.engine.applied_revision,
                **checkpoint_data,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", source_receipt)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.acquisition.starts == h.commands.restores == []
    assert not h.acquisition.streaming
    assert not any(item[0] == "stop_graph" for item in h.log)
    if strategy == "module":
        assert h.output_restarts == []
    assert h.profiles.repository.get_activation().revision == h.previous.revision
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["module", "rebuild"])
async def test_declared_live_source_resumes_without_replay_checkpoint(narrow, rebuild_harness, monkeypatch, strategy):
    h = narrow("module") if strategy == "module" else rebuild_harness()
    request = h.commands.request

    def live_receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "config_revision": h.engine.applied_revision,
                "supported": False, "checkpoint": None, "resume_without_checkpoint_safe": True,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", live_receipt)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.restores == []
    assert len(h.acquisition.starts) == 1
    assert h.acquisition.starts[0][0] == h.plan.target_revision
    assert h.engine.applied_revision == h.profiles.repository.get_activation().revision == h.plan.target_revision
    if strategy == "module":
        assert h.output_restarts == [h.plan.target_revision]
        assert_preserved(h)
    else:
        assert h.engine.starts == [h.plan.target_revision]
        assert operation["result"]["replay_resumed"] is False
        assert h.sessions.active_session_id != h.original_session


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"resume_without_checkpoint_safe": False}, {"resume_without_checkpoint_safe": None},
    {"generation": "another-generation"}, {"config_revision": "another-revision"},
    {"supported": True, "checkpoint": None},
])
async def test_same_source_replacement_must_reconfirm_live_resume_before_start(rebuild_harness, monkeypatch, invalid):
    h = rebuild_harness()
    request = h.commands.request
    inspected = []

    def receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            revision = h.engine.applied_revision
            inspected.append(revision)
            data = {"generation": h.engine.generation, "config_revision": revision,
                    "supported": False, "checkpoint": None, "resume_without_checkpoint_safe": True}
            if revision == h.plan.target_revision:
                data.update(invalid)
            return CommandResult("checkpoint", event_type.name, True, data=data)
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", receipt)
    operation = await apply(h)
    assert operation["status"] == "failed" and operation["rolled_back"]
    assert inspected == [h.previous.revision, h.plan.target_revision, h.previous.revision]
    assert h.commands.restores == []
    assert [revision for revision, _ in h.acquisition.starts] == [h.previous.revision]
    assert h.engine.applied_revision == h.profiles.repository.get_activation().revision == h.previous.revision


@pytest.mark.asyncio
async def test_live_rollback_also_requires_recreated_source_capability(rebuild_harness, monkeypatch):
    h = rebuild_harness()
    request = h.commands.request

    def receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "config_revision": h.engine.applied_revision,
                "supported": False, "checkpoint": None,
                "resume_without_checkpoint_safe": not h.engine.starts,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", receipt)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.acquisition.starts == []
    assert h.engine.starts == [h.plan.target_revision, h.previous.revision]
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_explicit_different_source_can_start_fresh_after_live_source(rebuild_harness, monkeypatch):
    h = rebuild_harness()
    selected = h.profiles.create("acquisition", h.profiles.catalog.legacy(
        "acquisition", "neuroscan_scan45_reader",
    ).selection, "Different source", {})
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.bindings]
    bindings[0] = {"slot": "acquisition.source", "profile_id": selected.profile_id}
    h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision)
    assert h.plan.strategy == "rebuild" and not h.plan.errors
    request = h.commands.request
    inspected = []

    def receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            inspected.append(h.engine.applied_revision)
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "config_revision": h.engine.applied_revision,
                "supported": False, "checkpoint": None,
                "resume_without_checkpoint_safe": h.engine.applied_revision == h.previous.revision,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", receipt)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert inspected == [h.previous.revision]
    assert h.acquisition.starts == [(h.plan.target_revision, 0)]
    assert h.commands.restores == []


@pytest.mark.asyncio
async def test_nonstreaming_source_without_checkpoint_can_update_outputs_without_start(narrow, monkeypatch):
    h = narrow("module")
    h.acquisition.streaming = False
    request = h.commands.request

    def unsupported(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "config_revision": h.engine.applied_revision,
                "supported": False, "checkpoint": None,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", unsupported)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.acquisition.starts == h.commands.restores == []
    assert h.output_restarts == [h.plan.target_revision]
    assert_preserved(h)


def with_unavailable_windows_trigger(h, monkeypatch):
    trigger = h.profiles.create(
        "trigger", h.profiles.catalog.legacy("trigger", "neuroscan_scan45_trigger").selection,
        "Windows parallel-port trigger", {},
    )
    get = h.profiles.catalog.get

    def unavailable_trigger(kind, selection):
        definition = get(kind, selection)
        return replace(definition, available=False) if kind == "trigger" else definition

    # Make the unsupported-platform condition deterministic on every CI OS.
    monkeypatch.setattr(h.profiles.catalog, "get", unavailable_trigger)
    bindings = [{"slot": item.slot, "profile_id": item.profile_id} for item in h.previous.bindings]
    bindings.append({"slot": "stimulation.trigger", "profile_id": trigger.profile_id})
    selected = h.profiles.propose(bindings, refresh_slots=[])
    h.profiles.accept(selected, expected_revision=h.previous.revision)
    h.previous = selected
    h.engine.applied_snapshot = {**h.profiles.materialize(selected), "CONFIG.generation": h.engine.generation}
    h.commands.processing_revision = h.commands.acquisition_revision = selected.revision
    return bindings


@pytest.mark.asyncio
async def test_inactive_windows_trigger_does_not_block_decoder_apply(narrow, monkeypatch):
    h = narrow("trial_boundary")
    bindings = with_unavailable_windows_trigger(h, monkeypatch)
    h.plan = h.runtime.plan(bindings, expected_revision=h.previous.revision,
                            refresh_slots=["processing.decoder"])
    assert not h.plan.errors
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.processing_revision == h.plan.target_revision
    assert_preserved(h)


@pytest.mark.parametrize("active_during_plan", [True, False])
def test_active_windows_trigger_is_rejected_at_plan_or_revalidation(narrow, monkeypatch, active_during_plan):
    h = narrow("trial_boundary")
    bindings = with_unavailable_windows_trigger(h, monkeypatch)
    stimulation = h.runtime.executor.stimulation
    stimulation.process_alive = active_during_plan
    plan = h.runtime.plan(bindings, expected_revision=h.previous.revision,
                          refresh_slots=["processing.decoder"])
    if active_during_plan:
        assert any("stimulation.trigger: unavailable" in error for error in plan.errors)
    else:
        assert not plan.errors
        stimulation.process_alive = True
        with pytest.raises(PluginSchemaError, match="stimulation.trigger: unavailable"):
            h.runtime.resolve_plan(plan.plan_id)
    assert h.commands.stage_targets == []
    assert_preserved(h)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_logger_replacement_preserves_source_processing_and_session(tmp_path, monkeypatch, unused_tcp_port):
    import asyncio
    import json
    from uuid import uuid4

    from omnibci.application.commands.acquisition_commands import ConnectDataProviderCommand
    from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
    from omnibci.application.services.engine_service import NeuroEngineService
    from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
    from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
    from omnibci.bootstrap import assemble, register_engine_listeners
    from omnibci.infrastructure.actuation.process_manager import ActuationProcessManager
    from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
    from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
    from tests.application.test_runtime_apply_integration import configured_profiles

    profiles, bindings = configured_profiles(tmp_path, monkeypatch)
    plugin = tmp_path / "plugins" / "effectors" / "alternate_logger"
    plugin.mkdir(parents=True)
    (plugin / "manifest.toml").write_text('''manifest_version=2
[package]
id="alternate_logger"
publisher="test"
version="1.0.0"
api_version="2"
[[contributions]]
id="default"
kind="effector"
plugin_type="alternate_logger"
entrypoint="effector:AlternateLogger"
[contributions.configuration]
schema="config.schema.json"
schema_version=1
''')
    (plugin / "effector.py").write_text(
        "from omnibci.infrastructure.plugins.effectors.logger.effector import LogEffector\n"
        "class AlternateLogger(LogEffector):\n"
        "    plugin_type = 'alternate_logger'\n"
    )
    (plugin / "config.schema.json").write_text(json.dumps({
        "type": "object", "properties": {}, "additionalProperties": False,
    }))
    original = profiles.create("effector", profiles.catalog.legacy("effector", "logger").selection, "Logger", {})
    alternate = profiles.create("effector", profiles.catalog.legacy("effector", "alternate_logger").selection,
                                "Alternate logger", {})
    bindings.append({"slot": "actuation.effector", "profile_id": original.profile_id})
    host = profiles.settings_source()
    host.update({"ACTUATION.enabled": True, "ACTUATION.auto_spawn": True,
                 "ACTUATION.server_port": unused_tcp_port})
    profiles.settings_source = lambda: host
    profiles.accept(profiles.propose(bindings, include_host_settings=True),
                    expected_revision=profiles.repository.get_activation().revision)
    manager = ActuationProcessManager()
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build(),
                                actuation_process_manager=manager, snapshot_provider=profiles.resolve_settings)
    await engine.startup()
    container = assemble(engine, InMemorySessionRepository(), admission=engine.admission,
                         actuation_process_manager=manager)
    register_engine_listeners(container)
    coordinator = RuntimeApplyCoordinator(engine, profiles, container.acquisition_service,
                                          container.session_service, container.stimulation_service,
                                          container.command_results)
    runtime = RuntimeConfigurationService(profiles, engine, executor=coordinator)
    try:
        await engine.handle_start_engine(StartEngineCommand())
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        session = await asyncio.to_thread(container.recording_service.start)
        await asyncio.sleep(.08)
        old_workers = (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid)
        old_actuation = manager._process.pid
        generation = engine.generation
        bindings[-1] = {"slot": "actuation.effector", "profile_id": alternate.profile_id}
        plan = runtime.plan(bindings, expected_revision=engine.applied_revision)
        assert not plan.errors and plan.strategy == "module"
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        operation = runtime.operation(operation["operation_id"])
        assert operation["status"] == "succeeded", operation
        assert operation["result"]["session_preserved"] is True
        assert manager._process.pid != old_actuation
        assert manager._process.is_alive()
        assert (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid) == old_workers
        assert engine.generation == generation
        assert container.session_service.active_session_id == session
        assert len(container.session_repository.find_all()) == 1
        assert engine.applied_revision == profiles.repository.get_activation().revision == plan.target_revision
        checkpoints = [json.loads(line) for line in (tmp_path / "restored.jsonl").read_text().splitlines()]
        assert len(checkpoints) == 1 and checkpoints[0]["cursor"] > 0
    finally:
        await runtime.wait_for_idle()
        await engine.handle_stop_engine(StopEngineCommand())
