"""Narrow updates preserve workers and reconcile uncertain receipts."""
from tests.plugin_configuration_helpers import section_settings
from dataclasses import replace

import pytest

from omnibci.application.events import CommandResult, StreamDrainReceipt
from omnibci.application.configuration.models import ActivationSet, ModuleActivation
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.command_result_service import CommandTimeoutError
from omnibci.shared.events import EventType
from tests.plugin_configuration_helpers import builtin_selection
from tests.application.test_runtime_apply_failures import (
    Commands, apply, assert_quarantined, harness as rebuild_harness,
)


class NarrowCommands(Commands):
    def __init__(self, engine, acquisition, log):
        super().__init__(engine, acquisition, log)
        self.processing_revision = engine.applied_revisions["processing"]
        self.acquisition_revision = engine.applied_revisions["acquisition"]
        self.stage_outcome = "applied"
        self.cancel_outcome = "cancelled"
        self.bad_context_receipt = self.bad_source_receipt = False
        self.stage_targets = []
        self.context_targets = []

    def request(self, event_type, payload=None, **kwargs):
        identity = {"generation": self.engine.generation, "engine_revision": self.engine.applied_revisions["engine"]}
        if event_type is EventType.DetachProcessingInput:
            assert payload["expected_revision"] == self.acquisition_revision
            data = StreamDrainReceipt(self.engine.generation, self.acquisition_revision, 9, 1, True,
                                      processing_revision=self.processing_revision,
                                      engine_revision=self.engine.applied_revisions["engine"]).to_dict()
        elif event_type is EventType.StageProcessingConfiguration:
            assert payload["expected_revision"] == self.processing_revision
            target = payload["settings_snapshot"]["CONFIG.module_revisions"]["processing"]
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
        else:
            return super().request(event_type, payload, **kwargs)
        self.log.append((event_type.name, payload))
        return CommandResult("confirmed", event_type.name, True, data=data)


@pytest.fixture
def narrow(rebuild_harness):
    def create(strategy):
        h = rebuild_harness()
        h.engine.actuation_process_alive = True
        h.commands = NarrowCommands(h.engine, h.acquisition, h.log)
        h.runtime.executor.commands = h.commands
        h.output_restarts = []

        def prepare():
            h.log.append(("attach_processing", h.engine.applied_revisions["processing"]))

        start_module = h.engine.start_module_for_configuration

        async def start_module_observed(module, snapshot):
            if module == "actuation":
                h.output_restarts.append(snapshot["CONFIG.module_revisions"]["actuation"])
            await start_module(module, snapshot)

        h.runtime.executor.signal_preparer = prepare
        h.engine.start_module_for_configuration = start_module_observed
        h.module = "processing"
        bindings = [{"slot": item.slot, "profile_id": item.profile_id}
                    for item in h.previous.modules[h.module].bindings]
        if strategy == "trial_boundary":
            decoder = h.profiles.repository.get_profile(bindings[0]["profile_id"])
            h.profiles.update(decoder.profile_id, name=decoder.name,
                              parameters={**decoder.parameters, "window_duration": 2.5},
                              expected_revision=decoder.revision)
        else:
            decoder = h.profiles.create("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA"),
                                        "Replacement decoder", {})
            bindings[0] = {"slot": "processing.decoder", "profile_id": decoder.profile_id}
        h.plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision,
                               refresh_slots=["processing.decoder"], settings=h.runtime.profiles.default_module_settings(h.module))
        assert not h.plan.errors
        assert h.plan.strategy == strategy
        return h
    return create


def assert_preserved(h):
    assert h.engine.starts == []
    assert h.engine.generation == "original-generation"


@pytest.mark.asyncio
async def test_trial_update_changes_revisions_without_pausing_or_replacing_workers(narrow):
    h = narrow("trial_boundary")
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.processing_revision == h.plan.target_revisions[h.module]
    assert h.commands.acquisition_revision == h.previous.modules["acquisition"].revision
    assert h.engine.applied_revisions[h.module] == h.profiles.repository.get_module(h.module).revision == h.plan.target_revisions[h.module]
    assert h.output_restarts == []
    assert h.commands.restores == h.acquisition.starts == []
    assert not any(item[0] == "drain" for item in h.log)
    assert_preserved(h)


@pytest.mark.asyncio
async def test_applied_pipeline_history_failure_is_visible_and_does_not_block_outputs(narrow, monkeypatch):
    h = narrow("trial_boundary")

    def unavailable_history(*args, **kwargs):
        raise OSError("History disk unavailable")

    monkeypatch.setattr(h.profiles.repository, "record_module_history", unavailable_history)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert operation["result"]["applied"] is True
    assert operation["result"]["history_saved"] is False
    assert "History disk unavailable" in operation["result"]["warning"]
    assert h.engine.applied_revisions["processing"] == h.plan.target_revisions["processing"]
    assert h.runtime.status()["recovery"] == {}
    with h.engine.admission.command("stimulation"):
        pass
    with h.engine.admission.command("actuation"):
        pass
    assert h.acquisition.streaming


@pytest.mark.asyncio
async def test_trial_timeout_cancelled_retains_old_pipeline_without_quarantine(narrow):
    h = narrow("trial_boundary")
    h.commands.stage_outcome = "timeout_cancelled"
    operation = await apply(h)
    assert operation["status"] == "failed"
    assert not operation["rolled_back"]
    assert h.commands.processing_revision == h.previous.modules[h.module].revision
    assert h.commands.acquisition_revision == h.previous.modules["acquisition"].revision
    assert h.engine.applied_revisions[h.module] == h.profiles.repository.get_module(h.module).revision == h.previous.modules[h.module].revision
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
    assert h.commands.stage_targets == [h.plan.target_revisions[h.module]]
    assert h.commands.processing_revision == h.plan.target_revisions[h.module]
    assert h.commands.acquisition_revision == h.previous.modules["acquisition"].revision
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_outcome", ["unknown", "wrong_command"])
async def test_trial_timeout_with_unconfirmed_cancel_quarantines(narrow, cancel_outcome):
    h = narrow("trial_boundary")
    h.commands.stage_outcome = "timeout_applied"
    h.commands.cancel_outcome = cancel_outcome
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.commands.stage_targets == [h.plan.target_revisions[h.module]]
    assert h.commands.acquisition_revision == h.previous.modules["acquisition"].revision
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stage"])
async def test_trial_receipt_mismatch_does_not_guess_rollback(narrow, fault):
    h = narrow("trial_boundary")
    if fault == "stage":
        h.commands.stage_outcome = "bad_receipt"
    else:
        h.commands.bad_source_receipt = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert h.commands.processing_revision == h.plan.target_revisions[h.module]
    assert h.commands.stage_targets == [h.plan.target_revisions[h.module]]
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["trial_boundary", "module"])
async def test_pipeline_engine_acknowledgement_failure_requires_recovery(narrow, monkeypatch, strategy):
    h = narrow(strategy)

    def fail_acknowledgement(snapshot, *, modules=None):
        assert h.commands.processing_revision == snapshot["CONFIG.module_revisions"]["processing"]
        h.engine.is_engine_ready = False
        raise RuntimeError("Engine lost readiness after Processing applied")

    def unexpected_save(*args, **kwargs):
        pytest.fail("An unconfirmed engine configuration must not be persisted")

    monkeypatch.setattr(h.engine, "acknowledge_configuration", fail_acknowledgement)
    monkeypatch.setattr(h.profiles.repository, "save_module", unexpected_save)
    operation = await apply(h)

    assert operation["status"] == "recovery_required"
    assert "engine acknowledgement failed" in operation["error"]
    assert not operation["rolled_back"]
    assert h.commands.stage_targets == [h.plan.target_revisions["processing"]]
    assert h.commands.processing_revision == h.plan.target_revisions["processing"]
    assert h.engine.applied_revisions["processing"] == h.previous.modules["processing"].revision
    assert h.profiles.repository.get_activation_set() == h.previous
    assert h.profiles.repository.list_module_history("processing") == []
    assert h.commands.restores == h.acquisition.starts == h.output_restarts == []
    if strategy == "module":
        assert h.acquisition.streaming
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("external_change", [False, True])
async def test_trial_persistence_failure_checks_selection_before_rollback(narrow, monkeypatch, external_change):
    h = narrow("trial_boundary")
    save = h.profiles.repository.save_module
    external = ModuleActivation(h.module, {"ALGORITHM.baseline_duration": 99.0}, h.previous.modules[h.module].bindings)

    def fail_save(snapshot, *, expected_revision):
        if external_change:
            save(external, expected_revision=expected_revision)
            raise ProfileRevisionConflict("Another writer selected a configuration")
        raise OSError("save failed before replacement")

    monkeypatch.setattr(h.profiles.repository, "save_module", fail_save)
    operation = await apply(h)
    if external_change:
        assert operation["status"] == "recovery_required"
        assert h.commands.stage_targets == [h.plan.target_revisions[h.module]]
        assert h.commands.processing_revision == h.plan.target_revisions[h.module]
        assert h.profiles.repository.get_module(h.module).revision == external.revision
        assert_quarantined(h)
    else:
        assert operation["status"] == "failed" and operation["rolled_back"]
        assert h.commands.stage_targets == [h.plan.target_revisions[h.module], h.previous.modules[h.module].revision]
        assert h.commands.processing_revision == h.previous.modules[h.module].revision
        assert h.commands.acquisition_revision == h.previous.modules["acquisition"].revision
        assert h.engine.applied_revisions[h.module] == h.previous.modules[h.module].revision
    assert_preserved(h)


@pytest.mark.asyncio
async def test_module_update_detaches_input_and_preserves_source_streaming(narrow):
    h = narrow("module")
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.output_restarts == []
    assert h.commands.context_targets == []
    assert h.commands.restores == h.acquisition.starts == []
    assert h.acquisition.streaming
    assert h.commands.stage_targets == [h.plan.target_revisions[h.module]]
    names = [item[0] for item in h.log]
    assert names.index("DetachProcessingInput") < names.index("StageProcessingConfiguration") < names.index("attach_processing")
    assert "GetSourceCheckpoint" not in names
    assert_preserved(h)


@pytest.mark.asyncio
async def test_processing_can_change_before_any_input_has_been_attached(narrow):
    h = narrow("module")
    h.runtime.executor.signal_detacher = lambda: StreamDrainReceipt(
        h.engine.generation, h.engine.applied_revisions["acquisition"], -1, 0, False,
        engine_revision=h.engine.applied_revisions["engine"])
    operation = await apply(h)
    assert operation["status"] == "succeeded", operation
    assert h.acquisition.streaming
    assert h.acquisition.starts == h.commands.restores == []


@pytest.mark.asyncio
async def test_module_save_failure_restores_old_pipeline_without_stopping_source(narrow, monkeypatch):
    h = narrow("module")

    def fail_save(*args, **kwargs):
        raise OSError("save failed")

    monkeypatch.setattr(h.profiles.repository, "save_module", fail_save)
    operation = await apply(h)
    assert operation["status"] == "failed" and operation["rolled_back"]
    assert h.output_restarts == []
    assert h.commands.context_targets == []
    assert h.commands.processing_revision == h.previous.modules[h.module].revision
    assert h.commands.acquisition_revision == h.previous.modules["acquisition"].revision
    assert h.commands.restores == h.acquisition.starts == []
    assert h.acquisition.streaming
    assert_preserved(h)


@pytest.mark.asyncio
async def test_module_unknown_attachment_never_recreates_outputs_or_rewinds(narrow):
    h = narrow("module")

    def unknown_attachment():
        raise CommandTimeoutError("attach-unknown", "AttachProcessingInput", .01)

    h.runtime.executor.signal_preparer = unknown_attachment
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.output_restarts == []
    assert h.commands.restores == h.acquisition.starts == []
    assert h.acquisition.streaming
    assert h.engine.applied_revisions[h.module] == h.profiles.repository.get_module(h.module).revision == h.plan.target_revisions[h.module]
    assert_quarantined(h)
    assert_preserved(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["module", "rebuild"])
@pytest.mark.parametrize("checkpoint_data", [
    {"resume_mode": "restart-only", "checkpoint": None},
    {"resume_mode": "invalid", "checkpoint": None},
    {"resume_mode": "checkpoint", "checkpoint": None},
    {"resume_mode": "live-current", "checkpoint": {}},
    {"checkpoint": {}},
])
async def test_streaming_source_without_confirmed_checkpoint_is_not_automatically_restarted(
    narrow, rebuild_harness, monkeypatch, strategy, checkpoint_data,
):
    h = narrow("module") if strategy == "module" else rebuild_harness()
    request = h.commands.request

    def source_receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "acquisition_revision": h.engine.applied_revisions["acquisition"],
                "engine_revision": h.engine.applied_revisions["engine"],
                **checkpoint_data,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", source_receipt)
    operation = await apply(h)
    if strategy == "module":
        assert operation["status"] == "succeeded"
        assert h.acquisition.streaming
        assert h.acquisition.starts == h.commands.restores == []
        assert not any(item[0] == "GetSourceCheckpoint" for item in h.log)
        assert_preserved(h)
        return
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.acquisition.starts == h.commands.restores == []
    assert not h.acquisition.streaming
    assert not any(item[0] == "stop_graph" for item in h.log)
    if strategy == "module":
        assert h.output_restarts == []
    assert h.profiles.repository.get_module(h.module).revision == h.previous.modules[h.module].revision
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
                "generation": h.engine.generation, "acquisition_revision": h.engine.applied_revisions["acquisition"],
                "engine_revision": h.engine.applied_revisions["engine"],
                "resume_mode": "live-current", "checkpoint": None,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", live_receipt)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.restores == []
    if strategy == "rebuild":
        assert h.acquisition.starts == [(h.plan.target_revisions[h.module], 0)]
    else:
        assert h.acquisition.starts == []
    assert h.engine.applied_revisions[h.module] == h.profiles.repository.get_module(h.module).revision == h.plan.target_revisions[h.module]
    if strategy == "module":
        assert h.output_restarts == []
        assert_preserved(h)
    else:
        assert h.engine.starts == [h.plan.target_revisions[h.module]]
        assert operation["result"]["replay_resumed"] is False
        assert h.acquisition.streaming


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"resume_mode": "restart-only"}, {"resume_mode": None},
    {"generation": "another-generation"}, {"acquisition_revision": "another-revision"}, {"engine_revision": "another-engine"},
    {"resume_mode": "checkpoint", "checkpoint": None},
])
async def test_same_source_replacement_must_reconfirm_live_resume_before_start(rebuild_harness, monkeypatch, invalid):
    h = rebuild_harness()
    request = h.commands.request
    inspected = []

    def receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            revision = h.engine.applied_revisions[h.module]
            inspected.append(revision)
            data = {"generation": h.engine.generation, "acquisition_revision": revision, "engine_revision": h.engine.applied_revisions["engine"],
                    "resume_mode": "live-current", "checkpoint": None}
            if revision == h.plan.target_revisions[h.module]:
                data.update(invalid)
            return CommandResult("checkpoint", event_type.name, True, data=data)
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", receipt)
    operation = await apply(h)
    assert operation["status"] == "failed" and operation["rolled_back"]
    assert inspected == [h.previous.modules[h.module].revision, h.plan.target_revisions[h.module], h.previous.modules[h.module].revision]
    assert h.commands.restores == []
    assert [revision for revision, _ in h.acquisition.starts] == [h.previous.modules[h.module].revision]
    assert h.engine.applied_revisions[h.module] == h.profiles.repository.get_module(h.module).revision == h.previous.modules[h.module].revision


@pytest.mark.asyncio
async def test_live_rollback_also_requires_recreated_source_capability(rebuild_harness, monkeypatch):
    h = rebuild_harness()
    request = h.commands.request

    def receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "acquisition_revision": h.engine.applied_revisions["acquisition"],
                "engine_revision": h.engine.applied_revisions["engine"],
                "checkpoint": None,
                "resume_mode": "live-current" if not h.engine.starts else "restart-only",
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", receipt)
    operation = await apply(h)
    assert operation["status"] == "recovery_required"
    assert not operation["rolled_back"]
    assert h.acquisition.starts == []
    assert h.engine.starts == [h.plan.target_revisions[h.module], h.previous.modules[h.module].revision]
    assert_quarantined(h)


@pytest.mark.asyncio
async def test_explicit_different_source_can_start_fresh_after_live_source(rebuild_harness, monkeypatch):
    h = rebuild_harness()
    selected = h.profiles.create("acquisition", builtin_selection("acquisition", "neuroscan_scan45_reader"), "Different source", {})
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in h.previous.modules[h.module].bindings]
    bindings[0] = {"slot": "acquisition.source", "profile_id": selected.profile_id}
    h.plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision, settings=h.runtime.profiles.default_module_settings(h.module))
    assert h.plan.strategy == "rebuild" and not h.plan.errors
    request = h.commands.request
    inspected = []

    def receipt(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            inspected.append(h.engine.applied_revisions[h.module])
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "acquisition_revision": h.engine.applied_revisions["acquisition"],
                "engine_revision": h.engine.applied_revisions["engine"],
                "checkpoint": None,
                "resume_mode": "live-current" if h.engine.applied_revisions[h.module] == h.previous.modules[h.module].revision else "restart-only",
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", receipt)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert inspected == [h.previous.modules[h.module].revision]
    assert h.acquisition.starts == [(h.plan.target_revisions[h.module], 0)]
    assert h.commands.restores == []


@pytest.mark.asyncio
async def test_nonstreaming_source_without_checkpoint_can_update_outputs_without_start(narrow, monkeypatch):
    h = narrow("module")
    h.acquisition.streaming = False
    request = h.commands.request

    def unsupported(event_type, payload=None, **kwargs):
        if event_type is EventType.GetSourceCheckpoint:
            return CommandResult("checkpoint", event_type.name, True, data={
                "generation": h.engine.generation, "acquisition_revision": h.engine.applied_revisions["acquisition"],
                "engine_revision": h.engine.applied_revisions["engine"],
                "resume_mode": "restart-only", "checkpoint": None,
            })
        return request(event_type, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", unsupported)
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.acquisition.starts == h.commands.restores == []
    assert h.output_restarts == []
    assert_preserved(h)


def with_unavailable_windows_trigger(h, monkeypatch):
    trigger = h.profiles.create(
        "trigger", builtin_selection("trigger", "neuroscan_scan45_trigger"),
        "Windows parallel-port trigger", {},
    )
    get = h.profiles.catalog.get

    def unavailable_trigger(kind, selection):
        definition = get(kind, selection)
        return replace(definition, available=False) if kind == "trigger" else definition

    # Make the unsupported-platform condition deterministic on every CI OS.
    monkeypatch.setattr(h.profiles.catalog, "get", unavailable_trigger)
    current = h.previous.modules["stimulation"]
    bindings = [{"slot": item.slot, "profile_id": item.profile_id} for item in current.bindings]
    bindings.append({"slot": "stimulation.trigger", "profile_id": trigger.profile_id})
    selected = h.profiles.propose("stimulation", bindings, refresh_slots=[], settings=h.profiles.default_module_settings("stimulation"))
    h.profiles.repository.save_module(selected, expected_revision=current.revision)
    h.previous = h.profiles.repository.get_activation_set()
    h.engine.applied_snapshot = {**h.profiles.materialize(h.previous), "CONFIG.generation": h.engine.generation}
    h.commands.processing_revision = h.previous.modules["processing"].revision
    h.commands.acquisition_revision = h.previous.modules["acquisition"].revision
    return [{"slot": item.slot, "profile_id": item.profile_id} for item in h.previous.modules[h.module].bindings]


@pytest.mark.asyncio
async def test_inactive_windows_trigger_does_not_block_decoder_apply(narrow, monkeypatch):
    h = narrow("trial_boundary")
    bindings = with_unavailable_windows_trigger(h, monkeypatch)
    h.plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision,
                            refresh_slots=["processing.decoder"], settings=h.runtime.profiles.default_module_settings(h.module))
    assert not h.plan.errors
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert h.commands.processing_revision == h.plan.target_revisions[h.module]
    assert_preserved(h)


@pytest.mark.parametrize("active_during_plan", [True, False])
def test_other_module_platform_validation_does_not_block_processing(narrow, monkeypatch, active_during_plan):
    h = narrow("trial_boundary")
    bindings = with_unavailable_windows_trigger(h, monkeypatch)
    stimulation = h.runtime.executor.stimulation
    stimulation.process_alive = active_during_plan
    plan = h.runtime.plan(h.module, bindings, expected_revision=h.previous.modules[h.module].revision,
                          refresh_slots=["processing.decoder"], settings=h.runtime.profiles.default_module_settings(h.module))
    assert not plan.errors
    stimulation.process_alive = True
    h.runtime.resolve_plan(plan.plan_id)
    with pytest.raises(PluginSchemaError, match="stimulation.trigger: unavailable"):
        h.profiles.validate_module(h.previous.modules["stimulation"], active_kinds={"trigger"})
    assert h.commands.stage_targets == []
    assert_preserved(h)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_logger_replacement_preserves_source_and_processing(tmp_path, monkeypatch, unused_tcp_port):
    import asyncio
    import json
    from uuid import uuid4

    from omnibci.application.commands.acquisition_commands import ConnectDataProviderCommand, StartStreamCommand
    from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
    from omnibci.application.services.engine_service import NeuroEngineService
    from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
    from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
    from omnibci.bootstrap import assemble, register_engine_listeners
    from omnibci.infrastructure.actuation.process_manager import ActuationProcessManager
    from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
    from tests.application.test_runtime_apply_integration import configured_profiles, wire_signal_preparation

    profiles, bindings = configured_profiles(tmp_path, monkeypatch)
    plugin = tmp_path / "plugins" / "effectors" / "alternate_logger"
    plugin.mkdir(parents=True)
    (plugin / "manifest.toml").write_text('''manifest_version=2
[package]
id="alternate_logger"
domain="actuation"
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
    original = profiles.create("effector", builtin_selection("effector", "logger"), "Logger", {})
    alternate = profiles.create("effector", builtin_selection("effector", "alternate_logger"),
                                "Alternate logger", {})
    bindings.append({"slot": "actuation.effector", "profile_id": original.profile_id})
    settings = {**profiles.default_module_settings("actuation"), "ACTUATION.auto_spawn": True,
                "ACTUATION.server_port": unused_tcp_port}
    profiles.repository.save_module(profiles.propose("actuation", [bindings[-1]], settings=settings), expected_revision=None)
    manager = ActuationProcessManager()
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build(),
                                actuation_process_manager=manager, snapshot_provider=lambda: profiles.materialize(profiles.repository.get_activation_set()))
    await engine.startup()
    container = assemble(engine, admission=engine.admission,
                         actuation_process_manager=manager)
    signal = wire_signal_preparation(profiles, engine, container)
    register_engine_listeners(container)
    coordinator = RuntimeApplyCoordinator(engine, profiles, container.acquisition_service,
                                          container.stimulation_service,
                                          container.command_results, signal_preparer=signal.prepare,
                                          signal_detacher=signal.detach)
    runtime = RuntimeConfigurationService(profiles, engine, executor=coordinator)
    try:
        await engine.handle_start_engine(StartEngineCommand())
        async with engine.runtime_transition("acquisition"):
            await engine.start_module_for_configuration("acquisition", engine.applied_snapshot)
        async with engine.runtime_transition("processing"):
            await engine.start_module_for_configuration("processing", engine.applied_snapshot)
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
        async with engine.runtime_transition("actuation"):
            await engine.start_module_for_configuration("actuation", engine.applied_snapshot)
        await asyncio.sleep(.08)
        old_workers = (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid)
        old_actuation = manager._process.pid
        generation = engine.generation
        bindings[-1] = {"slot": "actuation.effector", "profile_id": alternate.profile_id}
        plan = runtime.plan("actuation", [bindings[-1]], expected_revision=engine.applied_revisions["actuation"], settings=runtime.profiles.repository.get_module("actuation").settings)
        assert not plan.errors and plan.strategy == "module"
        operation = runtime.submit(plan.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        operation = runtime.operation(operation["operation_id"])
        assert operation["status"] == "succeeded", operation
        assert manager._process.pid != old_actuation
        assert manager._process.is_alive()
        assert (engine._engine.acquisition_worker.pid, engine._engine.signal_processing_worker.pid) == old_workers
        assert engine.generation == generation
        assert container.acquisition_service.is_streaming
        assert engine.applied_revisions["actuation"] == profiles.repository.get_module("actuation").revision == plan.target_revisions["actuation"]
        assert not (tmp_path / "restored.jsonl").exists()
    finally:
        await runtime.wait_for_idle()
        await engine.handle_stop_engine(StopEngineCommand())


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["rejected", "cancelled", "unknown"])
async def test_processing_module_stage_failure_restores_only_confirmed_old_pipeline(narrow, monkeypatch, outcome):
    from omnibci.application.services.command_result_service import CommandRejectedError
    h = narrow("module")
    original = h.commands.request

    def request(event, payload=None, **kwargs):
        if event is EventType.StageProcessingConfiguration and outcome == "rejected":
            raise CommandRejectedError(CommandResult("rejected", event.name, False, message="Candidate rejected"))
        return original(event, payload, **kwargs)

    monkeypatch.setattr(h.commands, "request", request)
    if outcome in {"cancelled", "unknown"}:
        h.commands.stage_outcome = "timeout_cancelled"
        h.commands.cancel_outcome = "cancelled" if outcome == "cancelled" else "unknown"
    operation = await apply(h)
    assert h.profiles.repository.get_module("processing") == h.previous.modules["processing"]
    assert h.profiles.repository.list_module_history("processing") == []
    if outcome == "unknown":
        assert operation["status"] == "recovery_required"
        assert h.acquisition.starts == []
        assert_quarantined(h)
    else:
        assert operation["status"] == "failed" and operation["rolled_back"]
        assert h.acquisition.starts == []
        assert h.acquisition.streaming
        assert ("attach_processing", h.previous.modules["processing"].revision) in h.log
        assert h.commands.processing_revision == h.previous.modules["processing"].revision
        with h.engine.admission.command():
            pass


@pytest.fixture
def output_update(rebuild_harness):
    h = rebuild_harness(actuation=True)
    h.module = "actuation"
    h.output_events = []
    h.fail_output_start = False

    async def stop_output():
        h.output_events.append(("stop", h.engine.applied_revisions["actuation"]))

    async def reconfigure(snapshot, modules):
        assert modules == ("actuation",)
        h.output_events.append(("reconfigure", snapshot["CONFIG.module_revisions"]["actuation"]))

    async def start_output(module, snapshot):
        assert module == "actuation"
        revision = snapshot["CONFIG.module_revisions"]["actuation"]
        h.output_events.append(("start", revision))
        if h.fail_output_start:
            raise RuntimeError("Candidate startup outcome unknown")

    h.engine.stop_actuation_for_configuration = stop_output
    h.engine.reconfigure_output_modules = reconfigure
    h.engine.start_module_for_configuration = start_output
    settings = {**h.previous.settings(), "ACTUATION.watchdog_timeout_s": 3.0}
    bindings = [{"slot": item.slot, "profile_id": item.profile_id} for item in h.previous.modules["actuation"].bindings]
    h.plan = h.runtime.plan("actuation", bindings, expected_revision=h.previous.modules["actuation"].revision, settings=section_settings(settings, "actuation"))
    assert h.plan.strategy == "module" and not h.plan.errors
    return h


@pytest.mark.asyncio
async def test_output_module_apply_never_commands_acquisition_or_processing(output_update):
    h = output_update
    operation = await apply(h)
    assert operation["status"] == "succeeded"
    assert [event[0] for event in h.log] == ["SettleActuation"]
    assert h.commands.restores == h.acquisition.starts == h.engine.starts == []
    assert h.profiles.repository.get_module("processing") == h.previous.modules["processing"]
    assert h.profiles.repository.get_module("acquisition") == h.previous.modules["acquisition"]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["stimulation", "actuation"])
@pytest.mark.parametrize("occupied", [False, True])
async def test_output_address_change_is_local_and_failed_bind_restores_previous_listener(
    rebuild_harness, unused_tcp_port_factory, module, occupied,
):
    import asyncio
    from unittest.mock import AsyncMock

    from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
    from tests.neuroengine.test_neuro_engine import _make_engine
    from tests.neuroengine.test_peer_gateway import FakeBus

    h = rebuild_harness()
    h.module = module
    for role in ("stimulation", "actuation"):
        selected = h.profiles.repository.get_module(role)
        settings = {**selected.settings, f"{role.upper()}.server_port": unused_tcp_port_factory()}
        h.profiles.repository.save_module(ModuleActivation(role, settings, selected.bindings),
                                         expected_revision=selected.revision)
    h.previous = h.profiles.repository.get_activation_set()
    h.engine.applied_snapshot = {**h.profiles.materialize(h.previous), "CONFIG.generation": h.engine.generation}
    original = h.engine.applied_snapshot
    bus = FakeBus()
    gateway = PeerGateway(bus)
    foundation = _make_engine(event_bus=bus, peer_gateway=gateway,
                              generation=h.engine.generation, engine_revision=h.previous.engine.revision)
    foundation._loop = asyncio.get_running_loop()
    foundation._started = True
    h.engine.reconfigure_output_modules = foundation.reconfigure_output_modules
    h.engine.stop_actuation_for_configuration = AsyncMock()
    blocker = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0) if occupied else None
    port = blocker.sockets[0].getsockname()[1] if blocker else unused_tcp_port_factory()
    selected = h.previous.modules[module]
    h.plan = h.runtime.plan(module, [{"slot": item.slot, "profile_id": item.profile_id} for item in selected.bindings],
                            settings={**selected.settings, f"{module.upper()}.server_port": port},
                            expected_revision=selected.revision)
    assert h.plan.strategy == "module" and not h.plan.errors
    await gateway.start()
    try:
        for role in ("stimulation", "actuation"):
            await foundation.ensure_output_ready(role, original)
        other = "actuation" if module == "stimulation" else "stimulation"
        unrelated = gateway.peer(other)
        operation = await apply(h)
        assert operation["status"] == ("failed" if occupied else "succeeded"), operation
        assert operation["rolled_back"] is occupied
        listener = gateway.peer(module)._transport._server
        expected_port = original[f"{module.upper()}.server_port"] if occupied else port
        assert listener.port == expected_port and gateway.peer(module).is_ready
        assert gateway.peer(other) is unrelated and unrelated.is_ready
        assert h.engine.generation == original["CONFIG.generation"]
        assert h.engine.applied_revisions["engine"] == h.previous.engine.revision
        assert h.engine.is_module_running("acquisition") and h.engine.is_module_running("processing")
        assert h.acquisition.streaming and not h.log and not h.engine.starts
        for role in ("acquisition", "processing", other):
            assert h.profiles.repository.get_module(role) == h.previous.modules[role]
        if occupied:
            assert h.profiles.repository.get_module(module) == selected
        else:
            assert h.profiles.repository.get_module(module).settings[f"{module.upper()}.server_port"] == port
    finally:
        await gateway.stop()
        if blocker:
            blocker.close()
            await blocker.wait_closed()


@pytest.mark.asyncio
async def test_unknown_output_start_never_recreates_previous_driver(output_update):
    h = output_update
    h.fail_output_start = True
    operation = await apply(h)
    assert operation["status"] == "recovery_required" and not operation["rolled_back"]
    assert [event[0] for event in h.output_events] == ["stop", "reconfigure", "start"]
    assert h.profiles.repository.get_module("actuation") == h.previous.modules["actuation"]
    assert h.profiles.repository.list_module_history("actuation") == []
    assert_quarantined(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_settles", [False, True])
async def test_output_persistence_failure_settles_candidate_before_rollback(output_update, monkeypatch, candidate_settles):
    h = output_update
    requested = h.commands.request
    settlements = []
    def request(event, payload=None, **kwargs):
        if event is EventType.SettleActuation:
            revision = h.engine.applied_revisions["actuation"]
            settlements.append(revision)
            if revision == h.plan.target_revisions["actuation"] and not candidate_settles:
                raise RuntimeError("Cannot confirm candidate outputs")
        return requested(event, payload, **kwargs)
    def fail_save(*args, **kwargs):
        raise OSError("Failed to save selected output")
    monkeypatch.setattr(h.commands, "request", request)
    monkeypatch.setattr(h.profiles.repository, "save_module", fail_save)
    operation = await apply(h)
    assert settlements == [h.previous.modules["actuation"].revision, h.plan.target_revisions["actuation"]]
    assert h.commands.restores == h.acquisition.starts == h.engine.starts == []
    if candidate_settles:
        assert operation["status"] == "failed" and operation["rolled_back"]
        assert [event[0] for event in h.output_events] == ["stop", "reconfigure", "start"] * 2
    else:
        assert operation["status"] == "recovery_required"
        assert [event[0] for event in h.output_events] == ["stop", "reconfigure", "start"]
        assert_quarantined(h)
