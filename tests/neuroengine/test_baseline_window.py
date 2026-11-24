"""
Tests for pre-trigger capture, the epoch window, and baseline correction.

Covers:
  - PreTriggerBuffer: rolling capacity, take_pre_trigger_data, disabled (capacity 0).
  - EpochProcessor: pre-trigger data kept separate from the decode window.
  - SignalProcessor: pre_trigger_samples derived from Processing baseline_duration.
"""
from __future__ import annotations

from pathlib import Path
from tests.processing_helpers import prepared_signal
import numpy as np
import pytest

from tests.processing_helpers import make_processor
from omnibci.domain.ports.algorithm import Decoder, Prediction
from omnibci.infrastructure.signal_processing.epoch.epoch_processor import EpochProcessor
from omnibci.infrastructure.signal_processing.epoch.pretrigger_buffer import PreTriggerBuffer
from omnibci.domain.signal import SignalChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk(window: np.ndarray) -> SignalChunk:
    """Wrap a (channels × samples) window as a SignalChunk with a trigger row.

    SignalChunk.data drops the last (trigger) row, so we stack a zero trigger row.
    """
    trigger_row = np.zeros((1, window.shape[1]))
    raw = np.concatenate([window, trigger_row], axis=0)
    return SignalChunk(timestamp=0, sequence_id=0, raw_data=raw)


class _ConstDecoder(Decoder):
    """Decodes immediately, echoing how many samples it was handed."""

    def setup(self):
        pass

    def teardown(self):
        pass

    def should_decode(self, window):
        return window.shape[1] > 0

    def decode(self, window, *, latest_raw_segment):
        return Prediction(target_id=window.shape[1])  # target_id == window length

    def begin_epoch(self):
        pass


class _CaptureDecoder(Decoder):
    """Records the exact array handed to decode() so filtering can be asserted."""

    def setup(self):
        pass

    def teardown(self):
        pass

    def __init__(self):
        super().__init__()
        self.seen = None

    def should_decode(self, window):
        return window.shape[1] > 0

    def decode(self, window, *, latest_raw_segment):
        self.seen = np.asarray(window).copy()
        return Prediction(target_id=0)

    def begin_epoch(self):
        pass


# ---------------------------------------------------------------------------
# PreTriggerBuffer
# ---------------------------------------------------------------------------

class TestPreTriggerBuffer:
    def test_keeps_only_trailing_capacity(self):
        buf = PreTriggerBuffer(capacity=3)
        buf.push(np.arange(2 * 5).reshape(2, 5).astype(float))  # 5 samples
        pre_trigger_data = buf.take_pre_trigger_data(extra=np.zeros((2, 0)))
        assert pre_trigger_data.shape == (2, 3)
        np.testing.assert_array_equal(pre_trigger_data, np.arange(2 * 5).reshape(2, 5)[:, -3:])

    def test_take_pre_trigger_data_combines_buffer_and_extra(self):
        buf = PreTriggerBuffer(capacity=4)
        buf.push(np.ones((2, 2)))                 # history: 2 samples
        extra = np.full((2, 1), 9.0)              # in-chunk pre-trigger: 1 sample
        pre_trigger_data = buf.take_pre_trigger_data(extra=extra)
        assert pre_trigger_data.shape == (2, 3)  # 2 + 1, under capacity 4
        np.testing.assert_array_equal(pre_trigger_data[:, -1:], extra)

    def test_capacity_zero_disables(self):
        buf = PreTriggerBuffer(capacity=0)
        buf.push(np.ones((2, 10)))
        pre_trigger_data = buf.take_pre_trigger_data(extra=np.ones((2, 3)))
        assert pre_trigger_data.shape == (2, 0)


# ---------------------------------------------------------------------------
# EpochProcessor
# ---------------------------------------------------------------------------

class TestEpochProcessorPreTriggerData:
    def test_decoder_sees_window_only_pre_trigger_data_excluded(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 7))
        pre_trigger_data = np.zeros((2, 3))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5, pre_trigger_data=pre_trigger_data)
        ok, clf = ep.force_decode_at_boundary()
        assert ok is True
        # _ConstDecoder echoes window length: pre-trigger data is not prepended.
        assert clf.target_id == 7

    def test_pre_trigger_data_stored_separately_from_window(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 7))
        pre_trigger_data = np.zeros((2, 3))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5, pre_trigger_data=pre_trigger_data)
        assert ep.pre_trigger_samples == 3
        assert ep.sample_count == 7
        np.testing.assert_array_equal(ep._pre_trigger_data, pre_trigger_data)
        np.testing.assert_array_equal(ep._epoch_data, window)

    def test_no_pre_trigger_data_leaves_window_unchanged(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 4))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5)
        assert ep.pre_trigger_samples == 0
        assert ep.sample_count == 4


# ---------------------------------------------------------------------------
# SignalProcessor sample-count derivation
# ---------------------------------------------------------------------------

class TestSignalProcessorPreTriggerSamples:
    def _processor(self, baseline_duration, sample_rate=1000):
        from unittest.mock import MagicMock
        return make_processor(
            detector=MagicMock(), decoder=MagicMock(), command_queue=MagicMock(),
            data_manager=MagicMock(), event_bus=MagicMock(),
            sample_rate=sample_rate, baseline_duration=baseline_duration,
        )

    def test_seconds_converted_to_samples(self):
        proc = self._processor(baseline_duration=0.2, sample_rate=1000)
        assert proc.pre_trigger_samples == 200
        assert proc.pretrigger.capacity == 200

    def test_zero_duration_disables(self):
        proc = self._processor(baseline_duration=0.0)
        assert proc.pre_trigger_samples == 0
        assert proc.pretrigger.capacity == 0


# ---------------------------------------------------------------------------
# BaselineCorrection filter
# ---------------------------------------------------------------------------

class TestBaselineCorrectionFilter:
    def _filter(self):
        from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import (
            BaselineCorrection,
        )
        return BaselineCorrection(resource_root=Path(__file__).parent, parameters={}, input_signal=prepared_signal(250))

    def test_subtracts_per_channel_baseline_mean(self):
        flt = self._filter()
        baseline = np.array([[1.0, 3.0], [10.0, 20.0]])      # means: 2, 15
        window = np.array([[5.0, 7.0], [30.0, 40.0]])
        out = flt.apply(window, pre_trigger_data=baseline)
        np.testing.assert_array_equal(out, np.array([[3.0, 5.0], [15.0, 25.0]]))

    def test_noop_when_baseline_none(self):
        flt = self._filter()
        window = np.ones((2, 4))
        out = flt.apply(window, pre_trigger_data=None)
        np.testing.assert_array_equal(out, window)

    def test_noop_when_baseline_empty(self):
        flt = self._filter()
        window = np.ones((2, 4))
        out = flt.apply(window, pre_trigger_data=np.zeros((2, 0)))
        np.testing.assert_array_equal(out, window)


class TestBaselineCorrectionInPipeline:
    def test_decoder_receives_corrected_window(self):
        dec = _CaptureDecoder()
        flt = self._make_filter()
        ep = EpochProcessor(decoder=dec, sample_rate=1000, filters=[flt])
        # Constant reference data estimates a baseline of 2.0: 5.0 - 2.0 = 3.0.
        window = np.full((2, 6), 5.0)
        pre_trigger_data = np.full((2, 3), 2.0)
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=1, pre_trigger_data=pre_trigger_data)
        ep.force_decode_at_boundary()
        np.testing.assert_array_equal(dec.seen, np.full((2, 6), 3.0))

    @staticmethod
    def _make_filter():
        from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import (
            BaselineCorrection,
        )
        return BaselineCorrection(resource_root=Path(__file__).parent, parameters={}, input_signal=prepared_signal(250))


if __name__ == "__main__":
    pytest.main(["-s", "-v", __file__])
