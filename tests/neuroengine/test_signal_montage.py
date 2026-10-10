"""Prepared channel mapping reaches every processing consumer before buffering."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import MagicMock

import numpy as np
import pytest

from omnibci.application.events import CommandRequest, StreamBarrier
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.domain.montage import ChannelDescriptor, SignalSetup, SourceMetadata, compile_signal_plan
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.neuroengine.processing import pipeline_configuration, signal_configuration
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.plugins.loader import PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.plugins.runtime_configuration import load_configured_plugin


def _plan(*, reference=True):
    source = SourceMetadata(
        "test-source", 250,
        tuple(ChannelDescriptor(i, name, unit="V") for i, name in enumerate(("O1", "O2", "M1"))),
        3, connection_id="connection-one", original_reference="Cz",
    )
    setup = SignalSetup.from_dict({
        "source_id": source.source_id, "outputs": ["O2", "O1"],
        "reference": {"mode": "single", "channels": ["M1"]} if reference else {"mode": "original"},
    })
    return compile_signal_plan(source, setup, config_revision="revision-one")


def _snapshot(plan):
    return {
        "CONFIG.revision": plan.config_revision, "CONFIG.generation": "generation-one",
        "ACQUISITION.sample_rate": plan.source.sample_rate,
        "ACQUISITION.channel_names": [c.name for c in plan.source.channels],
        "ALGORITHM.baseline_duration": 0.008,
        "SIGNAL.setup": plan.setup.to_dict(), "PLUGINS.bindings": [],
    }


def _receipt(plan):
    return {
        "generation": "generation-one", "config_revision": plan.config_revision,
        "connection_id": plan.source.connection_id,
        "source_fingerprint": plan.source.fingerprint, "plan_fingerprint": plan.fingerprint,
    }


def _request(plan, receipt=None):
    return CommandRequest("prepare-one", {"plan": plan.to_dict(), "receipt": receipt or _receipt(plan)})


class _Decoder(Decoder):
    def should_decode(self, window):
        return True

    def decode(self, window, latest):
        self.window = window.copy()
        self.latest = latest.copy()
        return None


class _Detector(EpochDetector):
    def __init__(self):
        self.seen = []

    def detect_start(self, chunk):
        self.seen.append(chunk)
        indices = np.flatnonzero(chunk.triggers == 1)
        return (int(indices[0]), 1) if len(indices) else (-1, 0)

    def detect_end(self, chunk):
        return (-1, 0)


class _Filter(Filter):
    def apply(self, data, ctx):
        self.baseline = ctx.baseline.copy()
        self.sample_rate = ctx.sample_rate
        return data


def _processor(plan):
    return SignalProcessor(
        detector=None, decoder=None, command_queue=MagicMock(),
        data_manager=MagicMock(), event_bus=MagicMock(),
        sample_rate=1000, baseline_duration=0.008, auto_start_session=False,
        generation="generation-one", config_revision=plan.config_revision,
        settings_snapshot=_snapshot(plan),
    )


def _prepare(monkeypatch, processor, plan):
    decoder, detector, signal_filter = _Decoder(), _Detector(), _Filter()
    load = MagicMock(return_value=(decoder, detector, [signal_filter]))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    processor._execute((SignalProcessorCommand.prepare_signal, _request(plan)))
    assert processor.event_bus.put_event.call_args.args[0].data.ok
    return decoder, detector, signal_filter, load


def test_worker_with_explicit_setup_starts_control_loop_before_plugin_construction(monkeypatch):
    from omnibci.conf import settings
    monkeypatch.setattr(settings, "use_snapshot", lambda _: None)
    plan = _plan()
    worker = SignalProcessingWorker(command_queue=MagicMock(), settings_snapshot=_snapshot(plan))
    worker._load_algorithm = MagicMock(side_effect=AssertionError("premature plugin construction"))
    worker._run_processor = MagicMock()
    worker.run()
    worker._load_algorithm.assert_not_called()
    worker._run_processor.assert_called_once_with(None, None, [], 1000)


def test_prepare_binds_observed_rate_and_is_idempotent(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    decoder, detector, _, load = _prepare(monkeypatch, processor, plan)
    assert processor._decoder is decoder
    assert processor.detector is detector
    assert processor._sample_rate == 250
    assert processor.baseline_samples == 2
    processor.pretrigger.push(np.array([[9.0], [7.0]]))
    buffer = processor.pretrigger
    processor._prepare_signal(_request(plan))
    assert load.call_count == 1
    assert processor.pretrigger is buffer
    assert processor.event_bus.put_event.call_args.args[0].data.data == _receipt(plan)


@pytest.mark.parametrize("field,value", [
    ("generation", "stale-generation"), ("config_revision", "stale-revision"),
    ("connection_id", "stale-connection"), ("plan_fingerprint", "wrong-plan"),
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
    changed = compile_signal_plan(plan.source, SignalSetup.from_dict({
        **plan.setup.to_dict(), "outputs": ["O1"],
    }), config_revision=plan.config_revision)
    load = MagicMock()
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    processor._prepare_signal(_request(changed))
    result = processor.event_bus.put_event.call_args.args[0].data
    assert not result.ok
    assert "accepted setup" in result.message
    load.assert_not_called()


def test_new_connection_cannot_replace_an_active_epoch(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    _, _, _, load = _prepare(monkeypatch, processor, plan)
    processor.current_epoch = MagicMock()
    reconnected = replace(plan, source=replace(plan.source, connection_id="connection-two"))
    processor._prepare_signal(_request(reconnected))
    assert not processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor._signal_receipt == _receipt(plan)
    assert load.call_count == 1


@pytest.mark.asyncio
async def test_data_and_session_start_are_rejected_before_preparation():
    plan = _plan()
    processor = _processor(plan)
    processor.data_manager.get_data.return_value = SignalChunk(10, 1, np.zeros((4, 2)))
    with pytest.raises(RuntimeError, match="not been prepared"):
        await processor.next_chunk()
    processor._execute((SignalProcessorCommand.start_session, CommandRequest("start", {"session_id": "test-session"})))
    assert not processor.event_bus.put_event.call_args.args[0].data.ok


@pytest.mark.asyncio
async def test_montage_precedes_detector_baseline_window_and_latest(monkeypatch):
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
    np.testing.assert_array_equal(signal_filter.baseline, [[29, 30], [9, 10]])
    np.testing.assert_array_equal(decoder.window, [[31, 32, 33], [11, 12, 13]])
    np.testing.assert_array_equal(decoder.latest, [[32, 33], [12, 13]])
    assert signal_filter.sample_rate == 250
    for observed, original in zip(detector.seen, chunks, strict=True):
        np.testing.assert_array_equal(observed.triggers, original.triggers)
        assert observed.timestamp == original.timestamp
        assert observed.sequence_id == original.sequence_id
        assert observed.data.shape[0] == 2
    for original, raw in zip(chunks, originals, strict=True):
        np.testing.assert_array_equal(original.raw_data, raw)


@pytest.mark.asyncio
async def test_stream_barrier_bypasses_signal_transform(monkeypatch):
    plan = _plan()
    processor = _processor(plan)
    _prepare(monkeypatch, processor, plan)
    processor._signal_plan = MagicMock()
    processor.data_manager.get_data.return_value = StreamBarrier(
        "drain", "generation-one", "revision-one", -1,
    )
    assert await processor.next_chunk() is None
    processor._signal_plan.apply.assert_not_called()
    assert processor.event_bus.put_event.call_args.args[0].data.ok


def test_input_context_preserves_acquired_names_and_describes_ordered_outputs():
    plan = _plan()
    context = signal_configuration.signal_context(_snapshot(plan), plan)
    assert context.channel_names == ("O1", "O2", "M1")
    assert tuple(c.name for c in context.input_signal.channels) == ("O2", "O1")
    assert context.input_signal.trigger_row_index == 2
    assert context.acquired_signal == plan.source
    assert context.input_signal.original_reference == "M1"
    assert context.sample_rate == 250


def test_different_bipolar_references_are_not_reported_as_one_common_reference():
    plan = _plan()
    setup = SignalSetup.from_dict({"source_id": plan.source.source_id, "reference": {"mode": "bipolar"}, "outputs": [
        {"name": "O1-M1", "positive": "O1", "negative": "M1"},
        {"name": "O2-O1", "positive": "O2", "negative": "O1"},
    ]})
    plan = compile_signal_plan(plan.source, setup, config_revision=plan.config_revision)
    context = signal_configuration.signal_context(_snapshot(plan), plan)
    assert context.input_signal.original_reference is None
    assert [c.reference for c in context.input_signal.channels] == ["M1", "O1"]


def _bindings():
    catalog = PluginCatalog()
    return [ProfileBinding(slot, kind, None, catalog.resolve_legacy(kind, alias).selection, parameters).to_dict()
            for slot, kind, alias, parameters in (
                ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {"window_duration": 1.0, "offset_time": 0.0}),
                ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
            )]


def test_legacy_plugin_requires_signal_contract_only_for_transformed_input():
    plan = _plan()
    snapshot = {**_snapshot(plan), "PLUGINS.bindings": _bindings()}
    descriptor = replace(PluginCatalog().resolve_legacy("decoder", "THUBCI_SSVEP_classic40CCA"), signal_input_contract=None)
    catalog = MagicMock()
    catalog.resolve_selection.return_value = descriptor
    loader = MagicMock()
    with pytest.raises(PluginLoadError, match="signal_input_contract"):
        load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                               context=signal_configuration.signal_context(snapshot, plan), catalog=catalog, loader=loader)
    loader.instantiate.assert_not_called()
    identity = compile_signal_plan(plan.source)
    load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder,
                           context=signal_configuration.signal_context(snapshot, identity), catalog=catalog, loader=loader)
    loader.instantiate.assert_called_once()


def test_trial_pipeline_replacement_receives_current_transformed_context(monkeypatch):
    plan = _plan()
    before = {**_snapshot(plan), "PLUGINS.bindings": _bindings()}
    after = deepcopy(before)
    after["CONFIG.revision"] = "revision-two"
    after["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 1.5
    load = MagicMock(return_value=_Decoder())
    monkeypatch.setattr(pipeline_configuration, "load_configured_plugin", load)
    pipeline_configuration.build_pipeline_update(before, after, sample_rate=250, baseline_duration=0.008, signal_plan=plan)
    context = load.call_args.kwargs["context"]
    assert context.config_revision == "revision-two"
    assert context.acquired_signal == plan.source
    assert context.input_signal.channels == plan.output_channels
    assert context.input_signal.original_reference == "M1"


@pytest.mark.parametrize("update", ["output_context", "trial_pipeline"])
def test_narrow_apply_updates_plan_revision_and_reprepare_preserves_active_buffers(monkeypatch, update):
    plan = _plan()
    processor = _processor(plan)
    processor._settings_snapshot["PLUGINS.bindings"] = _bindings()
    _, detector, _, preparation_load = _prepare(monkeypatch, processor, plan)
    processor.pretrigger.push(np.array([[9.0], [7.0]]))
    buffer = processor.pretrigger
    after = deepcopy(processor._settings_snapshot)
    after["CONFIG.revision"] = "revision-two"
    request = {"settings_snapshot": after, "expected_revision": plan.config_revision,
               "generation": "generation-one"}
    if update == "trial_pipeline":
        after["PLUGINS.bindings"][0]["parameters"]["window_duration"] = 1.5
        monkeypatch.setattr(pipeline_configuration, "load_configured_plugin", MagicMock(return_value=_Decoder()))
        processor._stage_configuration(CommandRequest("trial-update", request))
    else:
        processor._update_output_context(CommandRequest("output-update", request))
    assert processor.event_bus.put_event.call_args.args[0].data.ok
    assert processor._signal_plan.config_revision == "revision-two"
    assert processor._signal_plan.fingerprint == plan.fingerprint
    active_decoder = processor._decoder
    processor.current_epoch = MagicMock()
    processor._prepare_signal(_request(replace(plan, config_revision="revision-two")))
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
