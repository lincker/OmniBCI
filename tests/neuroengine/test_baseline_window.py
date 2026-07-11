"""
Tests for the baseline / window split added to the engine subsystem.

Covers:
  - Epoch VO: trigger_offset, baseline/window views, validation, equality.
  - PreTriggerBuffer: rolling capacity, take_baseline, disabled (capacity 0).
  - EpochProcessor: baseline kept separate from the decode window; build_epoch
    stitches [baseline | window] and sets trigger_offset.
  - SignalProcessor: baseline_samples derived from baseline_duration.
"""
from __future__ import annotations

import numpy as np
import pytest

from omnibci.domain.epoch import Epoch
from omnibci.domain.ports.algorithm import Decoder, Prediction
from omnibci.infrastructure.neuroengine.processing.epoch.epoch_processor import EpochProcessor
from omnibci.infrastructure.neuroengine.processing.epoch.pretrigger_buffer import PreTriggerBuffer
from omnibci.shared.signal import SignalChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk(window: np.ndarray) -> SignalChunk:
    """Wrap a (channels × samples) window as a SignalChunk with a trigger row.

    SignalChunk.data drops the last (trigger) row, so we stack a zero trigger row.
    """
    trigger_row = np.zeros((1, window.shape[1]))
    raw = np.concatenate([window, trigger_row], axis=0)
    return SignalChunk(timestamp=0.0, sequence_id=0, raw_data=raw)


class _ConstDecoder(Decoder):
    """Decodes immediately, echoing how many samples it was handed."""
    def should_decode(self, window):
        return window.shape[1] > 0

    def decode(self, window, latest=None):
        return Prediction(target_id=window.shape[1])  # target_id == window length

    def for_new_epoch(self):
        return self


class _CaptureDecoder(Decoder):
    """Records the exact array handed to decode() so filtering can be asserted."""
    def __init__(self):
        super().__init__()
        self.seen = None

    def should_decode(self, window):
        return window.shape[1] > 0

    def decode(self, window, latest=None):
        self.seen = np.asarray(window).copy()
        return Prediction(target_id=0)

    def for_new_epoch(self):
        return self


# ---------------------------------------------------------------------------
# Epoch VO
# ---------------------------------------------------------------------------

class TestEpochBaselineWindow:
    def test_default_trigger_offset_is_zero_and_window_is_whole(self):
        ep = Epoch(data=np.ones((2, 5)), event_id=1, sample_rate=1000, channel_count=2)
        assert ep.trigger_offset == 0
        assert ep.has_baseline is False
        assert ep.baseline.shape == (2, 0)
        assert ep.window.shape == (2, 5)

    def test_split_at_trigger_offset(self):
        data = np.arange(2 * 6).reshape(2, 6).astype(float)
        ep = Epoch(data=data, event_id=1, sample_rate=1000, channel_count=2, trigger_offset=2)
        assert ep.has_baseline is True
        np.testing.assert_array_equal(ep.baseline, data[:, :2])
        np.testing.assert_array_equal(ep.window, data[:, 2:])

    def test_trigger_offset_must_leave_a_window(self):
        with pytest.raises(ValueError):
            Epoch(data=np.ones((2, 4)), event_id=1, sample_rate=1000,
                  channel_count=2, trigger_offset=4)  # no window samples

    def test_negative_trigger_offset_rejected(self):
        with pytest.raises(ValueError):
            Epoch(data=np.ones((2, 4)), event_id=1, sample_rate=1000,
                  channel_count=2, trigger_offset=-1)

    def test_equality_includes_trigger_offset(self):
        d = np.ones((2, 4))
        a = Epoch(data=d.copy(), event_id=1, sample_rate=1000, channel_count=2, trigger_offset=1)
        b = Epoch(data=d.copy(), event_id=1, sample_rate=1000, channel_count=2, trigger_offset=2)
        assert a != b


# ---------------------------------------------------------------------------
# PreTriggerBuffer
# ---------------------------------------------------------------------------

class TestPreTriggerBuffer:
    def test_keeps_only_trailing_capacity(self):
        buf = PreTriggerBuffer(capacity=3)
        buf.push(np.arange(2 * 5).reshape(2, 5).astype(float))  # 5 samples
        base = buf.take_baseline(extra=np.zeros((2, 0)))
        assert base.shape == (2, 3)
        np.testing.assert_array_equal(base, np.arange(2 * 5).reshape(2, 5)[:, -3:])

    def test_take_baseline_combines_buffer_and_extra(self):
        buf = PreTriggerBuffer(capacity=4)
        buf.push(np.ones((2, 2)))                 # history: 2 samples
        extra = np.full((2, 1), 9.0)              # in-chunk pre-trigger: 1 sample
        base = buf.take_baseline(extra=extra)
        assert base.shape == (2, 3)               # 2 + 1, under capacity 4
        np.testing.assert_array_equal(base[:, -1:], extra)

    def test_capacity_zero_disables(self):
        buf = PreTriggerBuffer(capacity=0)
        buf.push(np.ones((2, 10)))
        base = buf.take_baseline(extra=np.ones((2, 3)))
        assert base.shape == (2, 0)


# ---------------------------------------------------------------------------
# EpochProcessor
# ---------------------------------------------------------------------------

class TestEpochProcessorBaseline:
    def test_decoder_sees_window_only_baseline_excluded(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 7))
        baseline = np.zeros((2, 3))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5, baseline=baseline)
        ok, clf = ep.run_pipeline(force_recognize=True)
        assert ok is True
        # _ConstDecoder echoes window length → confirms baseline was NOT prepended.
        assert clf.target_id == 7

    def test_build_epoch_stitches_baseline_and_sets_offset(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 7))
        baseline = np.zeros((2, 3))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5, baseline=baseline)
        epoch = ep.build_epoch()
        assert epoch.trigger_offset == 3
        assert epoch.data.shape == (2, 10)
        np.testing.assert_array_equal(epoch.baseline, baseline)
        np.testing.assert_array_equal(epoch.window, window)

    def test_no_baseline_is_backward_compatible(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 4))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5)
        epoch = ep.build_epoch()
        assert epoch.trigger_offset == 0
        assert epoch.data.shape == (2, 4)


# ---------------------------------------------------------------------------
# SignalProcessor sample-count derivation
# ---------------------------------------------------------------------------

class TestSignalProcessorBaselineSamples:
    def _processor(self, baseline_duration, sample_rate=1000):
        from unittest.mock import MagicMock
        from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessor
        from omnibci.domain.session import BCISession
        sess = BCISession()
        sess.start()
        return SignalProcessor(
            detector=MagicMock(), decoder=MagicMock(), command_queue=MagicMock(),
            data_manager=MagicMock(), event_bus=MagicMock(), session=sess,
            sample_rate=sample_rate, baseline_duration=baseline_duration,
        )

    def test_seconds_converted_to_samples(self):
        proc = self._processor(baseline_duration=0.2, sample_rate=1000)
        assert proc.baseline_samples == 200
        assert proc.pretrigger.capacity == 200

    def test_zero_duration_disables(self):
        proc = self._processor(baseline_duration=0.0)
        assert proc.baseline_samples == 0
        assert proc.pretrigger.capacity == 0


# ---------------------------------------------------------------------------
# BaselineCorrection filter
# ---------------------------------------------------------------------------

class TestBaselineCorrectionFilter:
    def _filter(self):
        from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import (
            BaselineCorrection,
        )
        return BaselineCorrection()

    def _ctx(self, baseline):
        from omnibci.domain.ports.algorithm import FilterContext
        return FilterContext(sample_rate=1000, baseline=baseline)

    def test_subtracts_per_channel_baseline_mean(self):
        flt = self._filter()
        baseline = np.array([[1.0, 3.0], [10.0, 20.0]])      # means: 2, 15
        window = np.array([[5.0, 7.0], [30.0, 40.0]])
        out = flt.apply(window, self._ctx(baseline))
        np.testing.assert_array_equal(out, np.array([[3.0, 5.0], [15.0, 25.0]]))

    def test_noop_when_baseline_none(self):
        flt = self._filter()
        window = np.ones((2, 4))
        out = flt.apply(window, self._ctx(None))
        np.testing.assert_array_equal(out, window)

    def test_noop_when_baseline_empty(self):
        flt = self._filter()
        window = np.ones((2, 4))
        out = flt.apply(window, self._ctx(np.zeros((2, 0))))
        np.testing.assert_array_equal(out, window)

    def test_registered_under_plugin_type(self):
        from omnibci.domain.ports.algorithm import Filter
        from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import (
            BaselineCorrection,
        )
        from omnibci.infrastructure.plugins.registry import registry_for
        assert registry_for(Filter).get("BASELINE_CORRECTION") is BaselineCorrection


class TestBaselineCorrectionInPipeline:
    def test_decoder_receives_corrected_window(self):
        dec = _CaptureDecoder()
        flt = self._make_filter()
        ep = EpochProcessor(decoder=dec, sample_rate=1000, filters=[flt])
        # window of constant 5.0, baseline of constant 2.0 → corrected to 3.0
        window = np.full((2, 6), 5.0)
        baseline = np.full((2, 3), 2.0)
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=1, baseline=baseline)
        ep.run_pipeline(force_recognize=True)
        np.testing.assert_array_equal(dec.seen, np.full((2, 6), 3.0))

    @staticmethod
    def _make_filter():
        from omnibci.infrastructure.plugins.algorithms.filters.baseline_correction import (
            BaselineCorrection,
        )
        return BaselineCorrection()


if __name__ == "__main__":
    pytest.main(["-s", "-v", __file__])
