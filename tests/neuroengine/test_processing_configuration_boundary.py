"""A staged pipeline changes only between epochs, with truthful receipts."""

from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.processing_helpers import marker_boundaries, make_processor
from omnibci.application.events import CommandFailureKind, CommandRequest
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.signal_processing import configuration_transactions as transactions_module
from omnibci.infrastructure.signal_processing import signal_processor as module
from omnibci.infrastructure.signal_processing.pipeline_configuration import ConfiguredPipeline
from omnibci.shared.events import EventType


class ValueDecoder(Decoder):
    def setup(self):
        pass

    def begin_epoch(self):
        pass

    def teardown(self):
        pass

    def should_decode(self, window):
        return True

    def decode(self, window, *, latest_raw_segment):
        return Prediction(int(window[0, 0]))


class Add(Filter):
    def __init__(self, value):
        self.value = value

    def apply(self, data, *, pre_trigger_data):
        return data + self.value


class Multiply(Filter):
    def __init__(self, value):
        self.value = value

    def apply(self, data, *, pre_trigger_data):
        return data * self.value


class Markers(EpochDetector):
    def detect(self, raw_data):
        return marker_boundaries(raw_data)


def _chunk(markers, sequence=1):
    return SignalChunk(1, sequence, np.vstack((np.ones(len(markers)), markers)))


@pytest.fixture
def configured(monkeypatch):
    events = []
    current = {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "old"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "run"}
    processor = make_processor(
        detector=Markers(), decoder=ValueDecoder(), filters=[Add(1)],
        command_queue=Queue(), data_manager=DataManager(buffer=Queue()),
        event_bus=SimpleNamespace(put_event=events.append),
        sample_rate=250, generation="run", processing_revision="old", settings_snapshot=current,
    )
    replacement = ConfiguredPipeline(ValueDecoder(), (Add(2), Multiply(3)),
                                      {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "new"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "run"})
    builder = MagicMock(return_value=replacement)
    monkeypatch.setattr(transactions_module, "build_pipeline_update", builder)
    return processor, events, replacement, builder


def _stage(processor, command_id="update", **overrides):
    payload = {"settings_snapshot": {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "new"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "run"},
               "expected_revision": "old", "generation": "run", "engine_revision": "engine", "boundary": "trial"}
    payload.update(overrides)
    processor._execute((module.SignalProcessorCommand.stage_configuration,
                        CommandRequest(command_id, payload)))


def _cancel(processor, target="update", generation="run"):
    processor._execute((module.SignalProcessorCommand.cancel_configuration,
                        CommandRequest("cancel", {"command_id": target, "generation": generation, "engine_revision": "engine"})))


def _results(events, command=None):
    return [event.data for event in events if event.type is EventType.CommandResult
            and (command is None or event.data.command == command)]


async def _feed(processor, markers, sequence=1):
    processor.data_manager.put_data(_chunk(markers, sequence))
    await processor._current_state.run()


def test_idle_update_acknowledges_only_after_objects_and_revision_are_installed(configured):
    processor, events, replacement, builder = configured
    _stage(processor)
    assert processor._decoder is replacement.decoder
    assert processor._filters == list(replacement.filters)
    assert processor.processing_revision == "new"
    assert processor.new_epoch_processor().processing_revision == "new"
    result, = _results(events)
    assert result.ok
    assert result.data == {"status": "applied", "command_id": "update",
                           "applied_revision": "new", "generation": "run", "engine_revision": "engine"}
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["same_chunk", "unexpected_start", "normal_completion"])
async def test_active_epoch_keeps_objects_and_revision_until_natural_boundary(configured, boundary):
    processor, events, replacement, _ = configured
    await _feed(processor, [7, 0, 0])
    old_epoch = processor.current_epoch
    old_decoder = processor._decoder
    _stage(processor)
    assert processor.processing_revision == "old"
    assert processor._decoder is old_decoder
    assert old_epoch.pipeline.filters[0].value == 1
    assert not _results(events)
    if boundary == "same_chunk":
        await _feed(processor, [0, 255, 0, 9, 0], 2)
    elif boundary == "unexpected_start":
        await _feed(processor, [0, 0, 9, 0], 2)
    else:
        await _feed(processor, [0, 0, 0], 2)
        assert processor.current_epoch is None
        await _feed(processor, [9, 0, 0], 3)
    assert processor.current_epoch is not old_epoch
    assert processor.current_epoch.decoder is replacement.decoder
    assert processor.current_epoch.processing_revision == "new"
    assert old_epoch.processing_revision == "old"
    assert processor.processing_revision == "new"
    trial_events = [event for event in events if event.type is EventType.TrialCompleted]
    assert trial_events[0].data.processing_revision == "old"
    assert trial_events[0].data.classification.target_id == 2
    receipt_event = next(event for event in events if event.type is EventType.CommandResult)
    assert events.index(trial_events[0]) < events.index(receipt_event)
    await _feed(processor, [0, 255, 0], 4)
    trials = [event.data for event in events if event.type is EventType.TrialCompleted]
    assert [(trial.processing_revision, trial.classification.target_id) for trial in trials] == [("old", 2), ("new", 9)]


def test_preparation_failure_retains_previous_objects_and_revision(configured):
    processor, events, _, builder = configured
    old_decoder = processor._decoder
    builder.side_effect = ValueError("invalid replacement")
    _stage(processor)
    assert processor._decoder is old_decoder
    assert processor.processing_revision == "old"
    assert processor._configuration_transactions.pending is None
    assert not _results(events)[0].ok


@pytest.mark.parametrize("overrides", [{"generation": "stale"}, {"expected_revision": "stale"}])
def test_stale_command_is_rejected_before_plugin_construction(configured, overrides):
    processor, events, _, builder = configured
    _stage(processor, **overrides)
    builder.assert_not_called()
    assert not _results(events)[0].ok
    assert processor.processing_revision == "old"


@pytest.mark.asyncio
async def test_one_pending_update_and_duplicate_id_never_construct_twice(configured):
    processor, events, _, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _stage(processor)
    assert not _results(events)
    _stage(processor, "other")
    assert not _results(events)[0].ok
    builder.assert_called_once()
    await _feed(processor, [0, 255, 0], 2)
    _stage(processor)
    replies = [result for result in _results(events) if result.command_id == "update"]
    assert len(replies) == 2 and replies[0] == replies[1]
    builder.assert_called_once()


@pytest.mark.asyncio
async def test_conflicting_duplicate_preserves_pending_update_until_boundary(configured):
    processor, events, replacement, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _stage(processor, settings_snapshot={"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "different"}, "CONFIG.engine_revision": "engine"})
    conflict, = _results(events)
    assert not conflict.ok
    assert conflict.error_code == "PROCESSING_CONFIGURATION_CONFLICT"
    assert conflict.failure_kind is CommandFailureKind.REJECTED
    assert conflict.data is None
    _stage(processor)
    assert _results(events) == [conflict]
    await _feed(processor, [0, 255, 9, 0], 2)
    assert processor.processing_revision == "new"
    assert processor.current_epoch.processing_revision == "new"
    assert processor.current_epoch.decoder is replacement.decoder
    assert _results(events)[-1].data["status"] == "applied"
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["applied", "rejected", "cancelled", "busy"])
async def test_conflicting_duplicate_preserves_terminal_receipt_and_original_retry(configured, outcome):
    processor, events, _, builder = configured
    if outcome in {"cancelled", "busy"}:
        await _feed(processor, [7, 0, 0])
    if outcome == "busy":
        _stage(processor, "waiting")
    elif outcome == "rejected":
        builder.side_effect = ValueError("invalid replacement")
    _stage(processor)
    if outcome == "cancelled":
        _cancel(processor)
    original, = [result for result in _results(events, EventType.StageProcessingConfiguration.name)
                 if result.command_id == "update"]
    calls = builder.call_count
    _stage(processor, settings_snapshot={"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "different"}, "CONFIG.engine_revision": "engine"})
    conflict = _results(events)[-1]
    assert not conflict.ok
    assert conflict.error_code == "PROCESSING_CONFIGURATION_CONFLICT"
    assert conflict.data is None
    _stage(processor)
    assert _results(events)[-1] == original
    assert builder.call_count == calls
    if outcome == "busy":
        await _feed(processor, [0, 255, 9, 0], 2)
        assert processor.processing_revision == "new"
        assert _results(events)[-1].command_id == "waiting"
        _stage(processor)
        assert _results(events)[-1] == original


def test_retry_ignores_dictionary_key_order(configured):
    processor, events, _, builder = configured
    _stage(processor)
    original, = _results(events)
    processor._execute((module.SignalProcessorCommand.stage_configuration, CommandRequest("update", {
        "generation": "run", "expected_revision": "old", "engine_revision": "engine", "boundary": "trial",
        "settings_snapshot": {"CONFIG.generation": "run", "CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "new"}, "CONFIG.engine_revision": "engine"},
    })))
    assert _results(events) == [original, original]
    builder.assert_called_once()


@pytest.mark.parametrize("invalid", [object(), float("nan")], ids=["not-json", "nan"])
def test_unfingerprintable_request_does_not_reserve_command_id(configured, invalid):
    processor, events, _, builder = configured
    _stage(processor, settings_snapshot={"invalid": invalid})
    rejected, = _results(events)
    assert rejected.error_code == "PROCESSING_CONFIGURATION_REJECTED"
    _stage(processor)
    assert _results(events)[-1].ok
    assert processor.processing_revision == "new"
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_unfingerprintable_duplicate_preserves_original_command(configured, pending):
    processor, events, _, builder = configured
    if pending:
        await _feed(processor, [7, 0, 0])
    _stage(processor)
    _stage(processor, settings_snapshot={"invalid": float("nan")})
    assert _results(events)[-1].error_code == "PROCESSING_CONFIGURATION_REJECTED"
    if pending:
        await _feed(processor, [0, 255, 9, 0], 2)
    original = next(result for result in _results(events) if result.ok)
    _stage(processor)
    assert _results(events)[-1] == original
    assert processor.processing_revision == "new"
    builder.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_cancel_before_boundary_keeps_old_configuration(configured):
    processor, events, _, _ = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _cancel(processor)
    stage, cancellation = _results(events)
    assert not stage.ok and stage.data["status"] == "cancelled"
    assert cancellation.ok and cancellation.data["applied_revision"] == "old"
    await _feed(processor, [0, 255, 9, 0], 2)
    assert processor.processing_revision == "old"


def test_timeout_cancel_after_install_reports_applied_and_never_rolls_back(configured):
    processor, events, _, _ = configured
    _stage(processor)
    _cancel(processor)
    cancellation = _results(events)[-1]
    assert cancellation.ok
    assert cancellation.data["status"] == "applied"
    assert cancellation.data["applied_revision"] == "new"
    assert processor.processing_revision == "new"


@pytest.mark.parametrize("snapshot", [
    {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "new"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "run"},
    {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": "different"}, "CONFIG.engine_revision": "engine"},
    {"invalid": float("nan")},
])
def test_cancel_tombstone_blocks_a_later_stage_with_same_id(configured, snapshot):
    processor, events, _, builder = configured
    _cancel(processor)
    cancelled = _results(events)[0]
    _stage(processor, settings_snapshot=snapshot)
    assert processor.processing_revision == "old"
    assert _results(events)[-1] == cancelled
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_early_cancellation_preserves_another_pending_update(configured):
    processor, events, _, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _cancel(processor, target="delayed")
    cancelled, acknowledgement = _results(events)
    assert cancelled.command_id == "delayed"
    assert cancelled.data["status"] == "cancelled"
    assert acknowledgement.ok
    await _feed(processor, [0, 255, 9, 0], 2)
    assert processor.processing_revision == "new"
    assert _results(events)[-1].command_id == "update"
    assert _results(events)[-1].data["status"] == "applied"
    _stage(processor, "delayed")
    assert _results(events)[-1] == cancelled
    builder.assert_called_once()


@pytest.mark.asyncio
async def test_stop_returns_cancelled_receipt_for_pending_update(configured):
    processor, events, _, _ = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    processor._execute((module.SignalProcessorCommand.should_stop, None))
    result, = _results(events)
    assert not result.ok
    assert result.data["status"] == "cancelled"
    assert processor._configuration_transactions.pending is None
    assert processor.processing_revision == "old"


def test_restart_generation_rejects_old_commands_and_pins_new_epochs(configured):
    processor, events, _, builder = configured
    processor.generation = "restarted"
    processor.processing_revision = "restart-revision"
    _stage(processor)
    assert not _results(events)[0].ok
    builder.assert_not_called()
    assert processor.new_epoch_processor().processing_revision == "restart-revision"


from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.channel_mapping import ChannelDescriptor, ProcessingInput, SignalInfo, compile_processing_plan
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
from omnibci.infrastructure.plugin_management.loader import PluginLoadError
from omnibci.infrastructure.signal_processing import pipeline_configuration as configuration
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot


def selected_snapshot(revision="old"):
    bindings = [ProfileBinding(slot, kind, None, builtin_selection(kind, alias), parameters,
                               processing_input=ProcessingInput() if kind == "decoder" else None).to_dict()
                for slot, kind, alias, parameters in (
                    ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 250}),
                    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA",
                     {"window_duration": 1.0, "offset_time": 0.0}),
                    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
                )]
    return {"CONFIG.module_revisions": {"acquisition": "source-revision", "processing": revision}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "run",
            "ACQUISITION.signal_source": "file_reader",
            "ACQUISITION.sample_rate": 250, "ALGORITHM.baseline_duration": 0.2,
            "PLUGINS.bindings": bindings}


def _build(before, after, boundary="module"):
    channels = tuple(ChannelDescriptor(index, name, signal_type="eeg", unit="V", channel_id=f"channel-{index}")
                     for index, name in enumerate(("O1", "O2")))
    source = SignalInfo(sample_rate=before["ACQUISITION.sample_rate"], channels=channels)
    plan = compile_processing_plan(source, ProcessingInput.from_dict(
        before["PLUGINS.bindings"][1]["processing_input"]), acquisition_revision=before["CONFIG.module_revisions"]["acquisition"])
    return configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=250, baseline_duration=0.2,
                                               signal_plan=plan, boundary=boundary)


def test_bundle_uses_new_parameters_and_ordered_distinct_filter_instances():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    selection = builtin_selection("filter", "BASELINE_CORRECTION")
    after["PLUGINS.bindings"].extend(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, selection, {},
    ).to_dict() for _ in range(2))
    bundle = _build(before, after)
    assert bundle.decoder.SAMPLE_COUNT == 375
    assert len(bundle.filters) == 2
    assert bundle.filters[0] is not bundle.filters[1]
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 9
    assert bundle.settings_snapshot["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1.5
    assert before["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1


def test_source_binding_position_does_not_change_processing_topology():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    source = after["PLUGINS.bindings"].pop(0)
    after["PLUGINS.bindings"].append(source)
    after["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 1.5
    bundle = _build(before, after, boundary="trial")
    assert bundle.decoder.SAMPLE_COUNT == 375


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
def test_trial_update_uses_prepared_source_rate_instead_of_legacy_flat_mirror(provenance):
    before = selected_snapshot()
    before["PLUGINS.bindings"][0]["parameters"].update(sample_rate=500, channel_count=3)
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    source = SignalInfo(sample_rate=500, channels=(
        ChannelDescriptor(0, "O1", signal_type="eeg", unit="V", channel_id="channel-0"),
        ChannelDescriptor(1, "O2", signal_type="eeg", unit="V", channel_id="channel-1"),
    ))
    plan = compile_processing_plan(source, ProcessingInput(), acquisition_revision="source-revision")

    bundle = configuration.build_pipeline_update(
        before, prepared_snapshot(after, ("processing",)), sample_rate=500, baseline_duration=0.2, signal_plan=plan,
    )

    assert bundle.decoder.SAMPLE_COUNT == 750
    assert bundle.settings_snapshot["ACQUISITION.sample_rate"] == 250


def test_unprepared_update_validates_settings_and_defers_plugin_construction(monkeypatch):
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    monkeypatch.setattr(configuration, "load_configured_plugin", lambda *a, **kw: pytest.fail("premature construction"))
    bundle = configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=None, baseline_duration=0.2, boundary="module")
    assert bundle.decoder is None and bundle.filters == ()
    assert bundle.settings_snapshot["CONFIG.module_revisions"]["processing"] == "new"
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = -1
    with pytest.raises(PluginLoadError):
        configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=None, baseline_duration=0.2, boundary="module")


def test_filter_can_be_applied_before_selecting_a_decoder(monkeypatch):
    before = selected_snapshot()
    before["PLUGINS.bindings"] = [before["PLUGINS.bindings"][0]]
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    selection = builtin_selection("filter", "BASELINE_CORRECTION")
    after["PLUGINS.bindings"].append(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, selection, {},
    ).to_dict())
    monkeypatch.setattr(configuration, "load_configured_plugin", lambda *a, **kw: pytest.fail("premature construction"))

    bundle = configuration.build_pipeline_update(before, prepared_snapshot(after, ("processing",)), sample_rate=None, baseline_duration=0.2, boundary="module")

    assert bundle.decoder is None and bundle.filters == ()
    assert bundle.settings_snapshot == prepared_snapshot(after, ("processing",))


@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", 500), ("ACQUISITION.channel_labels", ["Cz", "Trigger"]),
    ("ALGORITHM.baseline_duration", 0.3), ("CONFIG.generation", "another-run"),
])
def test_host_context_changes_require_restart(key, value):
    before = selected_snapshot()
    after = deepcopy(before)
    after[key] = value
    with pytest.raises(ValueError, match="restart"):
        _build(before, after)


@pytest.mark.parametrize("index", [0, 2])
def test_source_and_detector_binding_changes_require_restart(index):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][index]["parameters"]["changed"] = True
    with pytest.raises(ValueError, match="topology"):
        configuration.build_pipeline_update(before, after, sample_rate=None, baseline_duration=0.2)


@pytest.mark.parametrize("policy", [None, "module", "restart"])
def test_same_selection_parameter_changes_require_explicit_trial_policy(monkeypatch, policy):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    original_get = configuration.resolve_configured_plugin

    def get(snapshot, slot, kind):
        descriptor, parameters = original_get(snapshot, slot, kind)
        schema = deepcopy(descriptor.configuration_schema)
        declaration = schema["properties"]["window_duration"]
        declaration.pop("x-apply", None)
        if policy is not None:
            declaration["x-apply"] = policy
        return replace(descriptor, configuration_schema=schema), parameters

    monkeypatch.setattr(configuration, "resolve_configured_plugin", get)
    with pytest.raises(ValueError, match="restart"):
        _build(before, after, boundary="trial")


def test_decoder_replacement_and_filter_removal_build_complete_bundle():
    before = selected_snapshot()
    filter_selection = builtin_selection("filter", "BASELINE_CORRECTION")
    before["PLUGINS.bindings"].append(ProfileBinding(
        f"processing.filter.{uuid4()}", "filter", None, filter_selection, {},
    ).to_dict())
    after = deepcopy(before)
    after["PLUGINS.bindings"].pop()
    after["CONFIG.module_revisions"]["processing"] = "new"
    decoder = PluginCatalog().resolve_selection("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA"))
    after["PLUGINS.bindings"][1] = ProfileBinding(
        "processing.decoder", "decoder", None, decoder.selection,
        {"window_duration": 1.0, "offset_time": 0.0}, processing_input=ProcessingInput(),
    ).to_dict()
    bundle = _build(before, after)
    assert type(bundle.decoder).__name__ == "Classic40FBCCA"
    assert bundle.filters == ()


def test_invalid_candidate_does_not_change_input():
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = -1
    with pytest.raises(PluginLoadError):
        _build(before, after)
    assert before["PLUGINS.bindings"][1]["parameters"]["window_duration"] == 1


@pytest.mark.parametrize("changed_selection", [False, True])
def test_decoder_input_change_cannot_reuse_existing_prepared_plan(changed_selection):
    before = selected_snapshot()
    after = deepcopy(before)
    after["PLUGINS.bindings"][1]["processing_input"] = {"schema_version": 2, "outputs": [{"source_channel_id": "channel-1", "name": "O2"}]}
    if changed_selection:
        selection = builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA")
        after["PLUGINS.bindings"][1]["selection"] = selection.to_dict()
    with pytest.raises(ValueError, match="Processing input changes"):
        _build(before, after)


@pytest.mark.parametrize("change,message", [
    ("environment", "environment"), ("schema_version", "binding"),
])
def test_replacement_checks_prepared_contract_before_input_acknowledgement(monkeypatch, change, message):
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "new"
    replacement = PluginCatalog().resolve_selection("decoder", builtin_selection("decoder", "THUBCI_SSVEP_classic40FBCCA"))
    after["PLUGINS.bindings"][1]["selection"] = replacement.selection.to_dict()
    after = prepared_snapshot(after, ("processing",))
    if change == "environment":
        after["PLUGINS.artifacts"]["processing.decoder"]["environment_sha256"] = "0" * 64
    else:
        after["PLUGINS.bindings"][1]["configuration_version"] = 99
    monkeypatch.setattr(configuration, "load_configured_plugin", lambda *a, **kw: pytest.fail("premature construction"))
    with pytest.raises(PluginLoadError, match=message):
        configuration.build_pipeline_update(before, after, sample_rate=None, baseline_duration=0.2, boundary="module")


def test_output_changes_do_not_invalidate_processing_replacement():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"].update(processing="new", stimulation="new-stimulation", actuation="new-actuation")
    after.update({"STIMULATION.renderer": "another-renderer", "ACTUATION.max_command_age_s": 2.0})
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    after["PLUGINS.bindings"].append({
        "slot": "stimulation.paradigm", "kind": "paradigm", "profile_id": None,
        "selection": {"package_id": "remote.only", "contribution_id": "default", "version": "1.0.0"},
        "parameters": {}, "configuration_version": 1, "profile_revision": 0,
    })
    pipeline = _build(before, after)
    assert pipeline.decoder.SAMPLE_COUNT == 375
    assert before["CONFIG.module_revisions"]["processing"] == "old"


@pytest.mark.parametrize("field", ["CONFIG.engine_revision", "acquisition"])
def test_processing_replacement_rejects_changed_dependency(field):
    before = selected_snapshot()
    after = deepcopy(before)
    if field == "acquisition":
        after["CONFIG.module_revisions"][field] = "different-source"
    else:
        after[field] = "different-engine"
    with pytest.raises(ValueError, match="Source or Engine"):
        _build(before, after)


def test_module_boundary_preserves_result_epoch_and_source_sequence(configured):
    processor, events, replacement, builder = configured
    processor.completed_trials = 12
    processor._epoch_number = 17
    processor._last_sequence = 81
    _stage(processor, boundary="module")
    result, = _results(events)
    assert result.ok
    assert processor._decoder is replacement.decoder
    assert processor.processing_revision == "new"
    assert processor.acquisition_revision == "source-revision"
    assert (processor.completed_trials, processor._epoch_number, processor._last_sequence) == (12, 17, 81)
    assert builder.call_args.kwargs["boundary"] == "module"


@pytest.mark.asyncio
async def test_module_boundary_rejects_an_open_epoch_without_constructing_plugins(configured):
    processor, events, _, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor, boundary="module")
    result, = _results(events)
    assert not result.ok
    assert processor.processing_revision == "old"
    builder.assert_not_called()
