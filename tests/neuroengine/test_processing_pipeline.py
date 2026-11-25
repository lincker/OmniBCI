"""Contract tests for ProcessingPipeline filter and decoder composition."""
from __future__ import annotations

from typing import Optional

import numpy as np
import pytest

from omnibci.domain.ports.algorithm import Decoder, Filter, Prediction
from omnibci.infrastructure.signal_processing.epoch.pipeline import ProcessingPipeline


class _SpyDecoder(Decoder):
    def setup(self):
        pass

    def begin_epoch(self):
        pass

    def teardown(self):
        pass

    def __init__(self, ready=True, target=5):
        super().__init__()
        self._ready = ready
        self._target = target
        self.seen_data = None

    def should_decode(self, data) -> bool:
        return self._ready

    def decode(self, data, *, latest_raw_segment) -> Optional[Prediction]:
        # Two-arg port contract: decode(window, latest_raw_segment).
        self.seen_data = data
        self.seen_latest_raw_segment = latest_raw_segment
        if self._ready:
            return Prediction(target_id=self._target, confidence=0.9)
        return None


class _AddFilter(Filter):
    """Adds a constant; records the reference it was handed."""
    def __init__(self, k):
        super().__init__()
        self._k = k
        self.seen_pre_trigger_data = None

    def apply(self, data, *, pre_trigger_data):
        self.seen_pre_trigger_data = pre_trigger_data
        return data + self._k


@pytest.mark.parametrize("latest_kwargs", [{}, {"latest_raw_segment": None}], ids=["omitted", "none"])
def test_latest_raw_segment_must_be_supplied(latest_kwargs):
    decoder = _SpyDecoder()
    stage = _AddFilter(10)
    pipe = ProcessingPipeline(decoder=decoder, filters=[stage])

    with pytest.raises(TypeError, match="latest_raw_segment"):
        pipe.run(np.zeros((2, 3)), **latest_kwargs)

    assert decoder.seen_data is None
    assert stage.seen_pre_trigger_data is None


class TestPipelineNoFilters:
    def test_empty_pipeline_equals_direct_decode(self):
        dec = _SpyDecoder(ready=True, target=9)
        pipe = ProcessingPipeline(decoder=dec, filters=None)
        epoch = np.ones((3, 5))
        pred = pipe.run(epoch, latest_raw_segment=epoch[:, -1:])
        assert pred == Prediction(target_id=9, confidence=0.9)
        assert np.array_equal(dec.seen_data, epoch)   # decoder saw the unfiltered data

    def test_decline_returns_none(self):
        dec = _SpyDecoder(ready=False)
        pipe = ProcessingPipeline(decoder=dec)
        epoch = np.ones((2, 4))
        assert pipe.run(epoch, latest_raw_segment=epoch[:, -1:]) is None


class TestPipelineWithFilters:
    def test_filters_apply_in_order_before_decode(self):
        dec = _SpyDecoder()
        pipe = ProcessingPipeline(decoder=dec, filters=[_AddFilter(1), _AddFilter(10)])
        pipe.run(np.zeros((2, 3)), latest_raw_segment=np.zeros((2, 1)))
        assert np.all(dec.seen_data == 11)   # 0 + 1 + 10

    def test_supplied_latest_raw_segment_keep_input_values(self):
        dec = _SpyDecoder()
        pipe = ProcessingPipeline(decoder=dec, filters=[_AddFilter(10)])
        latest_raw_segment = np.ones((2, 1))
        pipe.run(np.zeros((2, 3)), latest_raw_segment=latest_raw_segment)
        assert np.all(dec.seen_data == 10)
        assert np.shares_memory(dec.seen_latest_raw_segment, latest_raw_segment)
        assert not dec.seen_latest_raw_segment.flags.writeable
        np.testing.assert_array_equal(dec.seen_latest_raw_segment, np.ones((2, 1)))

    def test_filter_receives_optional_pre_trigger_data(self):
        dec = _SpyDecoder()
        f = _AddFilter(0)
        pipe = ProcessingPipeline(decoder=dec, filters=[f])
        pipe.run(np.ones((2, 2)), latest_raw_segment=np.ones((2, 1)))
        assert f.seen_pre_trigger_data is None


from pathlib import Path
from types import SimpleNamespace
from omnibci.domain.ports import algorithm
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.plugins.algorithms.epoch.detector import DefaultEpochDetector
from omnibci.infrastructure.signal_processing.epoch.epoch_processor import EpochProcessor
from omnibci.infrastructure.signal_processing.processing_state import ProcessingState
from tests.processing_helpers import prepared_signal


class _ContractDecoder:
    def __init__(self):
        self.seen = []
        self.begins = 0

    def setup(self):
        pass

    def begin_epoch(self):
        self.begins += 1

    def should_decode(self, raw_window):
        assert not raw_window.flags.writeable
        return True

    def decode(self, filtered_window, *, latest_raw_segment):
        assert not filtered_window.flags.writeable and not latest_raw_segment.flags.writeable
        self.seen.append((filtered_window.copy(), latest_raw_segment.copy()))
        return algorithm.Prediction(1)

    def teardown(self):
        pass


def boundary(offset=0, kind="start", event_id=7):
    cls = getattr(algorithm, "EpochBoundary", None)
    assert cls is not None
    return cls(offset=offset, kind=kind, event_id=event_id)


def test_default_detector_returns_every_nonzero_marker_without_host_metadata():
    raw = np.array([[10., 11., 12., 13.], [7., 0., 9., 9.]])
    detector = DefaultEpochDetector(resource_root=Path(__file__).parent, parameters={}, input_signal=prepared_signal(250))
    result = detector.detect(raw)
    assert [(b.offset, b.kind, b.event_id) for b in result] == [(0, "start", 7), (2, "start", 9), (3, "start", 9)]
    assert detector.detect(raw[:, 1:2]) == ()


def test_host_calls_detector_once_with_borrowed_matrix_and_preserves_ties():
    raw = np.zeros((2, 6))
    expected = (boundary(1), boundary(3, "end", None), boundary(3, "start", 9))
    calls = []
    def detect(value):
        assert isinstance(value, np.ndarray) and np.shares_memory(value, raw)
        assert not value.flags.writeable
        calls.append(value)
        return expected
    state = ProcessingState(SimpleNamespace(detector=SimpleNamespace(detect=detect)))
    assert state._boundaries(SignalChunk(0, 0, raw)) == expected
    assert len(calls) == 1 and raw.flags.writeable


@pytest.mark.parametrize("case", ["outside", "order", "duplicate", "tie", "list", "wrong_type", "too_many"])
def test_invalid_boundary_batches_are_rejected_before_processing(case):
    start, end = boundary(1), boundary(1, "end", None)
    values = {"outside": (boundary(4),), "order": (boundary(2), start),
              "duplicate": (start, start), "tie": (start, end),
              "list": [start], "wrong_type": ((1, "start", 7),), "too_many": (start,) * 9}
    state = ProcessingState(SimpleNamespace(detector=SimpleNamespace(detect=lambda raw: values[case])))
    with pytest.raises((TypeError, ValueError), match="[Bb]oundar"):
        state._boundaries(SignalChunk(0, 0, np.zeros((2, 4))))


@pytest.mark.parametrize("output", [np.zeros((1, 4)), np.zeros((2, 3)), np.zeros(4),
                                   np.zeros((2, 4), dtype=bool), np.zeros((2, 4), dtype=complex),
                                   np.zeros((2, 4), dtype=object), [[0] * 4] * 2])
def test_bad_filter_outputs_do_not_reach_decoder(output):
    decoder = _ContractDecoder()
    stage = SimpleNamespace(apply=lambda data, *, pre_trigger_data: output)
    pipeline = ProcessingPipeline(decoder, [stage])
    with pytest.raises((TypeError, ValueError), match="[Ff]ilter"):
        pipeline.run(np.zeros((2, 4)), latest_raw_segment=np.zeros((2, 1)))
    assert decoder.seen == []


@pytest.mark.parametrize("target", ["data", "pre_trigger_data"])
def test_filter_cannot_modify_host_storage(target):
    data, reference = np.ones((2, 4)), np.ones((2, 2))
    def apply(value, *, pre_trigger_data):
        (value if target == "data" else pre_trigger_data)[0, 0] = 99
        return value
    pipeline = ProcessingPipeline(_ContractDecoder(), [SimpleNamespace(apply=apply)])
    with pytest.raises(ValueError, match="read-only"):
        pipeline.run(data, latest_raw_segment=data[:, -1:], pre_trigger_data=reference)
    np.testing.assert_array_equal(data, np.ones((2, 4)))
    np.testing.assert_array_equal(reference, np.ones((2, 2)))
    assert data.flags.writeable and reference.flags.writeable


def test_filters_share_original_reference_and_decoder_receives_latest_raw_segment():
    data, reference = np.ones((2, 4)), np.full((2, 2), 5.)
    decoder, seen = _ContractDecoder(), []
    def apply(value, *, pre_trigger_data):
        assert not value.flags.writeable and not pre_trigger_data.flags.writeable
        assert np.shares_memory(pre_trigger_data, reference)
        seen.append(pre_trigger_data.copy())
        return (value + 2).astype(np.float32)
    ProcessingPipeline(decoder, [SimpleNamespace(apply=apply)] * 2).run(
        data, latest_raw_segment=data[:, :0], pre_trigger_data=reference)
    assert len(seen) == 2
    np.testing.assert_array_equal(decoder.seen[0][0], np.full((2, 4), 5.))
    assert decoder.seen[0][1].shape == (2, 0)


def test_epoch_reset_occurs_for_repeated_event_ids_and_gate_receives_readonly_raw_window():
    decoder = _ContractDecoder()
    chunk = SignalChunk(0, 0, np.array([[3., 4.], [7., 0.]]))
    for _ in range(2):
        epoch = EpochProcessor(decoder)
        epoch.initialize_epoch(chunk, 0, 7)
        assert epoch.decode_if_ready()[0]
        assert not epoch.decode_if_ready()[0]
    assert decoder.begins == 2 and len(decoder.seen) == 2


def test_empty_window_skips_all_plugin_calls():
    decoder = _ContractDecoder()
    stage = SimpleNamespace(apply=lambda *args, **kwargs: pytest.fail("empty filter call"))
    assert ProcessingPipeline(decoder, [stage]).run(np.zeros((2, 0)), latest_raw_segment=np.zeros((2, 0))) is None
    assert decoder.seen == []


@pytest.mark.parametrize("result", [None, 1, "ready", np.array([True]), object()])
def test_invalid_readiness_result_fails_before_decoding(result):
    decoder = _ContractDecoder()
    decoder.should_decode = lambda window: result
    epoch = EpochProcessor(decoder)
    epoch.initialize_epoch(SignalChunk(0, 0, np.zeros((2, 3))), 0, 7)
    with pytest.raises(TypeError, match="should_decode.*bool"):
        epoch.decode_if_ready()
    assert decoder.seen == []


@pytest.mark.parametrize("result", [False, True, np.bool_(False), np.bool_(True)])
def test_readiness_accepts_boolean_scalars(result):
    decoder = _ContractDecoder()
    decoder.should_decode = lambda window: result
    epoch = EpochProcessor(decoder)
    epoch.initialize_epoch(SignalChunk(0, 0, np.zeros((2, 3))), 0, 7)
    assert epoch.should_decode() is bool(result)
