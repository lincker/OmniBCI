"""Prepared channel mapping reaches every processing consumer before buffering."""
from tests.plugin_configuration_helpers import builtin_selection
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.processing_helpers import make_processor
from omnibci.application.events import CommandRequest, ProcessingInputStart, StreamBarrier
from omnibci.application.configuration.models import ProfileBinding
from omnibci.domain.channel_mapping import (
    ChannelBinding, ProcessingInput, compile_processing_plan,
)
from omnibci.application.source_metadata import source_descriptor, source_fingerprint
from omnibci.domain.ports.acquisition import SourceObservation
from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, EpochBoundary, Filter
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.signal_processing import pipeline_configuration, signal_configuration
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog






def _plan():
    source = SignalInfo(sample_rate=250, channels=tuple(
        ChannelDescriptor(i, name, signal_type="eeg", unit="V", channel_id=f"channel-{i}")
        for i, name in enumerate(("O1", "O2", "M1"))))
    return compile_processing_plan(source, ProcessingInput(outputs=(
        ChannelBinding("O2", "channel-1"), ChannelBinding("O1", "channel-0"))),
        acquisition_revision="revision-one")


def _observation(plan):
    return SourceObservation(native_text="test source", signal_info=plan.source,
                             native_event="final-row", provenance="protocol-defined")


def _snapshot(plan):
    from omnibci.application.plugin_identity import PluginSelection
    return {
        "CONFIG.module_revisions": {"acquisition": plan.acquisition_revision, "processing": plan.acquisition_revision, "stimulation": plan.acquisition_revision, "actuation": plan.acquisition_revision}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "generation-one",
        "ACQUISITION.sample_rate": plan.source.sample_rate,
        "ACQUISITION.signal_source": "test-source",
        "ALGORITHM.baseline_duration": 0.008,
        "PLUGINS.bindings": [ProfileBinding("processing.decoder", "decoder", None,
            PluginSelection("test.decoder", "default", "1"), {},
            processing_input=plan.processing_input).to_dict()],
    }


def _receipt(plan):
    return {
        "generation": "generation-one", "acquisition_revision": plan.acquisition_revision, "processing_revision": "revision-one", "engine_revision": "engine",
        "connection_id": "connection-one",
        "source_fingerprint": source_fingerprint(source_descriptor(_observation(plan), source_id="test-source", connection_id="connection-one")), "plan_fingerprint": plan.fingerprint, "input_id": "test-input",
    }


def _request(plan, receipt=None):
    return CommandRequest("prepare-one", {"plan": plan.to_dict(), "receipt": receipt or _receipt(plan)})


class _Decoder(Decoder):
    def setup(self):
        pass

    def begin_epoch(self):
        pass

    def teardown(self):
        pass

    def should_decode(self, window):
        return True

    def decode(self, window, *, latest_raw_segment):
        self.window = window.copy()
        self.latest_raw_segment = latest_raw_segment.copy()
        return None


class _Detector(EpochDetector):
    def __init__(self):
        self.seen = []

    def detect(self, raw_data):
        self.seen.append(raw_data)
        return tuple(EpochBoundary(offset=int(index), kind="start", event_id=1)
                     for index in np.flatnonzero(raw_data[-1] == 1))


class _Filter(Filter):
    def apply(self, data, *, pre_trigger_data):
        self.baseline = pre_trigger_data.copy()
        return data


def _processor(plan):
    return make_processor(
        detector=None, decoder=None, command_queue=MagicMock(),
        data_manager=MagicMock(), event_bus=MagicMock(),
        sample_rate=1000, baseline_duration=0.008,
        generation="generation-one", processing_revision=plan.acquisition_revision, acquisition_revision=plan.acquisition_revision, engine_revision="engine",
        settings_snapshot=_snapshot(plan),
    )


def _prepare(monkeypatch, processor, plan):
    decoder, detector, signal_filter = _Decoder(), _Detector(), _Filter()
    load = MagicMock(return_value=(decoder, detector, [signal_filter]))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    processor._execute((SignalProcessorCommand.prepare_signal, _request(plan)))
    assert processor.event_bus.put_event.call_args.args[0].data.ok
    processor._on_input_start(ProcessingInputStart("attach", _receipt(plan), -1))
    return decoder, detector, signal_filter, load


def test_worker_with_explicit_setup_starts_control_loop_before_plugin_construction(monkeypatch):
    plan = _plan()
    worker = SignalProcessingWorker(command_queue=MagicMock(), settings_snapshot=_snapshot(plan))
    load = MagicMock(side_effect=AssertionError("premature plugin construction"))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    worker._run_processor = MagicMock()
    worker.run()
    load.assert_not_called()
    worker._run_processor.assert_called_once_with()



def test_prepare_binds_observed_rate_and_is_idempotent(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    decoder, detector, _, load = _prepare(monkeypatch, processor, plan)
    assert processor._decoder is decoder
    assert processor.detector is detector
    assert processor._sample_rate == 250
    assert processor.pre_trigger_samples == 2
    processor.pretrigger.push(np.array([[9.0], [7.0]]))
    buffer = processor.pretrigger
    processor._prepare_signal(_request(plan))
    assert load.call_count == 1
    assert processor.pretrigger is buffer
    assert processor.event_bus.put_event.call_args.args[0].data.data == _receipt(plan)


@pytest.mark.parametrize("field,value", [
    ("generation", "stale-generation"), ("acquisition_revision", "stale-revision"),
    ("processing_revision", "stale-processing"), ("engine_revision", "stale-engine"),
    ("connection_id", ""), ("connection_id", True), ("plan_fingerprint", "wrong-plan"),
])
def test_stale_preparation_does_not_construct_or_replace_plugins(monkeypatch, field, value):
    plan = _plan()
    processor = _processor(plan)
    load = MagicMock(side_effect=AssertionError("stale plan loaded"))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    receipt = _receipt(plan)
    receipt[field] = value
    processor._prepare_signal(_request(plan, receipt))
    result = processor.event_bus.put_event.call_args.args[0].data
    assert not result.ok
    assert result.error_code == "SIGNAL_PREPARATION_FAILED"
    assert processor._signal_plan is None
    assert processor._decoder is None
    load.assert_not_called()


def test_preparation_cannot_substitute_an_unaccepted_setup(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    changed = compile_processing_plan(plan.source, ProcessingInput(outputs=(
        ChannelBinding("O1", "channel-0"),)), acquisition_revision=plan.acquisition_revision)
    load = MagicMock()
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    processor._prepare_signal(_request(changed))
    result = processor.event_bus.put_event.call_args.args[0].data
    assert not result.ok
    assert "selected processing input" in result.message
    load.assert_not_called()




@pytest.mark.parametrize("mismatch", ["input", "source-fingerprint"])
def test_native_preparation_rejects_unaccepted_input_or_malformed_receipt(monkeypatch, mismatch):
    plan = _plan()
    processor = _processor(plan)
    if mismatch == "input":
        plan = compile_processing_plan(plan.source, ProcessingInput(outputs=(ChannelBinding("M1", "channel-2"),)), plan.acquisition_revision)
    receipt = _receipt(plan)
    if mismatch == "source-fingerprint":
        receipt["source_fingerprint"] = "invalid"
    load = MagicMock()
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    processor._prepare_signal(_request(plan, receipt))
    assert not processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor._signal_plan is None
    load.assert_not_called()


def test_new_connection_cannot_replace_an_active_epoch(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    _, _, _, load = _prepare(monkeypatch, processor, plan)
    processor.current_epoch = MagicMock()
    receipt = {**_receipt(plan), "connection_id": "connection-two"}
    processor._prepare_signal(_request(plan, receipt))
    assert not processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor._signal_receipt == _receipt(plan)
    assert load.call_count == 1


@pytest.mark.asyncio
async def test_data_is_rejected_before_preparation():
    plan = _plan()
    processor = _processor(plan)
    processor.data_manager.get_data.return_value = SignalChunk(10, 1, np.zeros((4, 2)))
    with pytest.raises(RuntimeError, match="not been prepared"):
        await processor.next_chunk()


@pytest.mark.asyncio
async def test_source_only_standby_drains_raw_data_and_can_prepare_later(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    processor._settings_snapshot["PLUGINS.bindings"] = []
    raw = SignalChunk(10, 3, np.array([[10, 11], [30, 31], [2, 2], [0, 1]]))
    later = SignalChunk(20, 4, np.array([[12, 13], [32, 33], [2, 2], [0, 0]]))
    processor.data_manager.get_data.side_effect = [
        raw, StreamBarrier("drain", "generation-one", "revision-one", 3, engine_revision="engine"), later,
    ]

    await processor._current_state.run()
    assert processor._last_sequence == 3
    assert processor.current_epoch is None
    await processor._current_state.run()
    result = processor.event_bus.put_event.call_args.args[0].data
    assert result.ok
    assert result.data["last_sequence"] == 3

    processor._install_configuration(_snapshot(plan))
    _, detector, _, _ = _prepare(monkeypatch, processor, plan)
    await processor._current_state.run()
    assert processor._last_sequence == 4
    assert len(detector.seen) == 1
    np.testing.assert_array_equal(detector.seen[0][:-1], [[32, 33], [12, 13]])


@pytest.mark.asyncio
@pytest.mark.parametrize("mapping", ["implicit", "explicit", "renamed", "typed"])
@pytest.mark.parametrize("dtype", [np.int16, np.float32])
async def test_matching_physical_channels_reuse_chunk_with_effective_input_context(monkeypatch, mapping, dtype):
    source = _plan().source
    if mapping == "typed":
        source = replace(source, channels=tuple(
            replace(channel, signal_type="unknown") for channel in source.channels))
    outputs = () if mapping == "implicit" else tuple(
        ChannelBinding(
            f"Input {channel.name}" if mapping == "renamed" else channel.name,
            channel.channel_id, signal_type="eeg" if mapping == "typed" else None,
        ) for channel in source.channels)
    plan = compile_processing_plan(source, ProcessingInput(outputs=outputs), "revision-one")
    processor = _processor(plan)
    _prepare(monkeypatch, processor, plan)
    raw = np.array([[10, 11], [30, 31], [2, 3], [0, 7]], dtype=dtype)
    chunk = SignalChunk(12345, 42, raw)
    processor.data_manager.get_data.return_value = chunk

    observed = await processor.next_chunk()

    assert observed is chunk
    assert observed.raw_data is raw
    assert observed.raw_data.dtype == dtype
    np.testing.assert_array_equal(observed.triggers, [0, 7])
    assert observed.timestamp == 12345
    assert observed.sequence_id == 42
    assert processor._last_sequence == 42
    context = signal_configuration.mapped_input(plan)
    assert context.channels == plan.output_channels
    if mapping in {"renamed", "typed"}:
        assert not plan.is_identity
        assert context is not source
        if mapping == "renamed":
            assert tuple(channel.name for channel in context.channels) == (
                "Input O1", "Input O2", "Input M1")
            assert tuple(channel.name for channel in source.channels) == ("O1", "O2", "M1")
        else:
            assert all(channel.signal_type == "eeg" for channel in context.channels)
            assert all(channel.signal_type == "unknown" for channel in source.channels)
    else:
        assert plan.is_identity
        assert context is source


@pytest.mark.asyncio
@pytest.mark.parametrize("indices", [(0, 2), (2, 0, 1)], ids=["ordered-subset", "reordered-all"])
async def test_subset_or_reordered_channels_still_copy_selected_rows_and_events(monkeypatch, indices):
    source = _plan().source
    outputs = tuple(ChannelBinding(source.channels[index].name, source.channels[index].channel_id)
                    for index in indices)
    plan = compile_processing_plan(source, ProcessingInput(outputs=outputs), "revision-one")
    processor = _processor(plan)
    _prepare(monkeypatch, processor, plan)
    raw = np.array([[10, 11], [30, 31], [2, 3], [0, 7]], dtype=np.int16)
    chunk = SignalChunk(12345, 42, raw)
    processor.data_manager.get_data.return_value = chunk

    observed = await processor.next_chunk()

    assert observed is not chunk
    assert not np.shares_memory(observed.raw_data, raw)
    np.testing.assert_array_equal(observed.raw_data, raw[[*indices, 3], :])
    assert observed.raw_data.dtype == raw.dtype
    assert observed.timestamp == chunk.timestamp
    assert observed.sequence_id == chunk.sequence_id


@pytest.mark.asyncio
async def test_channel_mapping_precedes_detector_pretrigger_window_and_latest_raw_segment(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    decoder, detector, signal_filter, _ = _prepare(monkeypatch, processor, plan)
    chunks = [
        SignalChunk(10, 0, np.array([[10, 11], [30, 31], [2, 2], [0, 0]])),
        SignalChunk(20, 1, np.array([[12, 13], [32, 33], [2, 2], [0, 1]])),
        SignalChunk(30, 2, np.array([[14, 15], [34, 35], [2, 2], [0, 0]])),
    ]
    originals = [chunk.raw_data.copy() for chunk in chunks]
    processor.data_manager.get_data.side_effect = chunks
    await processor._current_state.run()
    await processor._current_state.run()
    await processor._current_state.run()
    np.testing.assert_array_equal(signal_filter.baseline, [[31, 32], [11, 12]])
    np.testing.assert_array_equal(decoder.window, [[33, 34, 35], [13, 14, 15]])
    np.testing.assert_array_equal(decoder.latest_raw_segment, [[34, 35], [14, 15]])
    for observed, original in zip(detector.seen, chunks, strict=True):
        np.testing.assert_array_equal(observed[-1], original.triggers)
        assert observed[:-1].shape[0] == 2
    for original, raw in zip(chunks, originals, strict=True):
        np.testing.assert_array_equal(original.raw_data, raw)


@pytest.mark.asyncio
async def test_bound_channels_enter_epoch_before_ordered_filters_and_decoder(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    calls = []

    class BaselineFilter(Filter):
        def apply(self, data, *, pre_trigger_data):
            calls.append("baseline")
            np.testing.assert_array_equal(pre_trigger_data, [[31, 32], [11, 12]])
            np.testing.assert_array_equal(data, [[33, 34, 35], [13, 14, 15]])
            return data - pre_trigger_data.mean(axis=1, keepdims=True)

    class ScaleFilter(Filter):
        def apply(self, data, *, pre_trigger_data):
            calls.append("scale")
            np.testing.assert_array_equal(data, [[1.5, 2.5, 3.5], [1.5, 2.5, 3.5]])
            return data * 2

    class OrderedDecoder(_Decoder):
        def decode(self, window, *, latest_raw_segment):
            calls.append("decoder")
            return super().decode(window, latest_raw_segment=latest_raw_segment)

    decoder, detector = OrderedDecoder(), _Detector()
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", MagicMock(
        return_value=(decoder, detector, [BaselineFilter(), ScaleFilter()])))
    processor._execute((SignalProcessorCommand.prepare_signal, _request(plan)))
    assert processor.event_bus.put_event.call_args.args[0].data.ok
    processor._on_input_start(ProcessingInputStart("attach", _receipt(plan), -1))
    chunks = [
        SignalChunk(10, 0, np.array([[10, 11], [30, 31], [2, 2], [0, 0]])),
        SignalChunk(20, 1, np.array([[12, 13], [32, 33], [2, 2], [0, 1]])),
        SignalChunk(30, 2, np.array([[14, 15], [34, 35], [2, 2], [0, 0]])),
    ]
    processor.data_manager.get_data.side_effect = chunks
    for _ in chunks:
        await processor._current_state.run()

    assert calls == ["baseline", "scale", "decoder"]
    epoch = processor.current_epoch.build_epoch()
    np.testing.assert_array_equal(epoch.pre_trigger_data, [[31, 32], [11, 12]])
    np.testing.assert_array_equal(epoch.window, [[33, 34, 35], [13, 14, 15]])
    np.testing.assert_array_equal(decoder.window, [[3, 5, 7], [3, 5, 7]])
    np.testing.assert_array_equal(decoder.latest_raw_segment, [[34, 35], [14, 15]])
    assert epoch.event_id == 1
    assert epoch.channel_count == 2
    assert epoch.sample_rate == 250
    for observed, source in zip(detector.seen, chunks, strict=True):
        np.testing.assert_array_equal(observed[-1], source.triggers)


@pytest.mark.asyncio
async def test_stream_barrier_bypasses_signal_transform(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    _prepare(monkeypatch, processor, plan)
    processor._signal_plan = MagicMock()
    processor.data_manager.get_data.return_value = StreamBarrier(
        "drain", "generation-one", "revision-one", -1,
        engine_revision="engine")
    assert await processor.next_chunk() is None
    processor._signal_plan.apply.assert_not_called()
    assert processor.event_bus.put_event.call_args.args[0].data.ok


@pytest.mark.parametrize("duplicate_names", [False, True])
def test_input_context_preserves_live_channels_and_describes_ordered_aliases(duplicate_names):
    plan = _plan()
    source = plan.source
    if duplicate_names:
        source = replace(source, channels=tuple(
            replace(channel, name="O1") if channel.index == 1 else channel
            for channel in source.channels))
    plan = compile_processing_plan(source, ProcessingInput(outputs=(
        ChannelBinding("posterior-right", "channel-1"), ChannelBinding("posterior-left", "channel-0"))),
        acquisition_revision=plan.acquisition_revision)
    context = signal_configuration.mapped_input(plan)
    assert tuple(c.name for c in context.channels) == ("posterior-right", "posterior-left")
    assert tuple(c.channel_id for c in context.channels) == ("channel-1", "channel-0")
    assert tuple(c.index for c in context.channels) == (0, 1)
    assert not hasattr(context, "source_id")
    assert not hasattr(context, "connection_id")
    assert context.signal_channel_count == 2
    assert context.trigger_row_index == 2
    assert context.sample_rate == source.sample_rate




def _bindings():
    catalog = PluginCatalog()
    return [ProfileBinding(slot, kind, None, builtin_selection(kind, alias), parameters,
                          processing_input=_plan().processing_input if kind == "decoder" else None).to_dict()
            for slot, kind, alias, parameters in (
                ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {"window_duration": 1.0, "offset_time": 0.0}),
                ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
            )]


def test_trial_pipeline_replacement_receives_current_transformed_context(monkeypatch):
    plan = _plan()
    before = {**_snapshot(plan), "PLUGINS.bindings": _bindings()}
    after = deepcopy(before)
    after["CONFIG.module_revisions"]["processing"] = "revision-two"
    after["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 1.5
    load = MagicMock(return_value=_Decoder())
    monkeypatch.setattr(pipeline_configuration, "load_configured_plugin", load)
    pipeline_configuration.build_pipeline_update(before, after, sample_rate=250, baseline_duration=0.008, signal_plan=plan)
    context = load.call_args.kwargs["input_signal"]
    assert context.channels == plan.output_channels


def test_processing_update_preserves_source_identity_and_reprepare_buffers(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    processor._settings_snapshot["PLUGINS.bindings"] = _bindings()
    _, detector, _, preparation_load = _prepare(monkeypatch, processor, plan)
    processor.pretrigger.push(np.array([[9.0], [7.0]]))
    buffer = processor.pretrigger
    after = deepcopy(processor._settings_snapshot)
    after["CONFIG.module_revisions"]["processing"] = "revision-two"
    after["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 1.5
    monkeypatch.setattr(pipeline_configuration, "load_configured_plugin", MagicMock(return_value=_Decoder()))
    processor._stage_configuration(CommandRequest("trial-update", {
        "settings_snapshot": after, "expected_revision": "revision-one",
        "generation": "generation-one", "engine_revision": "engine", "boundary": "trial",
    }))
    assert processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor._signal_plan.acquisition_revision == "revision-one"
    assert processor._signal_plan.fingerprint == plan.fingerprint
    active_decoder = processor._decoder
    processor.current_epoch = MagicMock()
    receipt = {**_receipt(plan), "processing_revision": "revision-two"}
    processor._prepare_signal(_request(plan, receipt))
    assert processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor._decoder is active_decoder
    assert processor.detector is detector
    assert processor.pretrigger is buffer
    assert preparation_load.call_count == 1


def test_bundled_pipeline_constructs_with_prepared_sample_rate():
    plan = _plan()
    snapshot = {**_snapshot(plan), "PLUGINS.bindings": _bindings()}
    decoder, detector, filters = signal_configuration.load_signal_pipeline(snapshot, plan)
    assert decoder.SAMPLE_COUNT == 250
    assert detector.plugin_type == "DEFAULT_EPOCH_DETECTOR"
    assert filters == []


def test_configured_processor_does_not_allocate_baseline_from_expected_rate():
    processor = _processor(_plan())
    assert processor._sample_rate is None
    assert processor.pre_trigger_samples == 0
    assert processor.pretrigger.capacity == 0
