"""
Tests for pre-trigger capture, the epoch window, and baseline correction.

Covers:
  - Epoch VO: pre_trigger_data/window views, validation, equality.
  - PreTriggerBuffer: rolling capacity, take_pre_trigger_data, disabled (capacity 0).
  - EpochProcessor: pre-trigger data kept separate from the decode window;
    build_epoch stitches [pre_trigger_data | window] and sets trigger_offset.
  - SignalProcessor: pre_trigger_samples derived from legacy baseline_duration.
"""
from __future__ import annotations

import numpy as np
import pytest

from omnibci.domain.epoch import Epoch
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
    return SignalChunk(timestamp=0.0, sequence_id=0, raw_data=raw)


class _ConstDecoder(Decoder):
    """Decodes immediately, echoing how many samples it was handed."""
    def should_decode(self, window):
        return window.shape[1] > 0

    def decode(self, window, latest_samples=None):
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

    def decode(self, window, latest_samples=None):
        self.seen = np.asarray(window).copy()
        return Prediction(target_id=0)

    def for_new_epoch(self):
        return self


# ---------------------------------------------------------------------------
# Epoch VO
# ---------------------------------------------------------------------------

class TestEpochPreTriggerWindow:
    def test_default_trigger_offset_is_zero_and_window_is_whole(self):
        ep = Epoch(data=np.ones((2, 5)), event_id=1, sample_rate=1000, channel_count=2)
        assert ep.trigger_offset == 0
        assert ep.has_pre_trigger_data is False
        assert ep.pre_trigger_data.shape == (2, 0)
        assert ep.window.shape == (2, 5)

    def test_split_at_trigger_offset(self):
        data = np.arange(2 * 6).reshape(2, 6).astype(float)
        ep = Epoch(data=data, event_id=1, sample_rate=1000, channel_count=2, trigger_offset=2)
        assert ep.has_pre_trigger_data is True
        np.testing.assert_array_equal(ep.pre_trigger_data, data[:, :2])
        assert np.shares_memory(ep.pre_trigger_data, ep.data)
        assert not ep.pre_trigger_data.flags.writeable
        np.testing.assert_array_equal(ep.window, data[:, 2:])
        assert ep.duration_samples == 6
        assert ep.duration_seconds == 0.006

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

    def test_build_epoch_stitches_pre_trigger_data_and_sets_offset(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 7))
        pre_trigger_data = np.zeros((2, 3))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5, pre_trigger_data=pre_trigger_data)
        epoch = ep.build_epoch()
        assert epoch.trigger_offset == 3
        assert epoch.data.shape == (2, 10)
        np.testing.assert_array_equal(epoch.pre_trigger_data, pre_trigger_data)
        np.testing.assert_array_equal(epoch.window, window)

    def test_no_pre_trigger_data_leaves_window_unchanged(self):
        ep = EpochProcessor(decoder=_ConstDecoder(), sample_rate=1000)
        window = np.ones((2, 4))
        ep.initialize_epoch(_chunk(window), start_idx=0, event_id=5)
        epoch = ep.build_epoch()
        assert epoch.trigger_offset == 0
        assert epoch.data.shape == (2, 4)


# ---------------------------------------------------------------------------
# SignalProcessor sample-count derivation
# ---------------------------------------------------------------------------

class TestSignalProcessorPreTriggerSamples:
    def _processor(self, baseline_duration, sample_rate=1000):
        from unittest.mock import MagicMock
        from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor
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
        return BaselineCorrection()


if __name__ == "__main__":
    pytest.main(["-s", "-v", __file__])


# ---------------------------------------------------------------------------
# P2-10: Epoch 构造不再有副作用（不改调用者数组 + 自身只读）
# ---------------------------------------------------------------------------

class TestEpochConstructionSideEffects:
    def test_caller_array_stays_writeable(self):
        """构造 Epoch 不得就地冻结调用者传入的数组。"""
        src = np.ones((2, 5))
        assert src.flags.writeable
        Epoch(data=src, event_id=1, sample_rate=1000, channel_count=2)
        assert src.flags.writeable          # 旧实现下此处会变 False
        src[0, 0] = 42.0                     # 调用者仍可自由写自己的 buffer
        assert src[0, 0] == 42.0

    def test_epoch_data_is_read_only_copy(self):
        """Epoch 持有的是自有只读副本（值对象不可变契约保留）。"""
        src = np.ones((2, 5))
        ep = Epoch(data=src, event_id=1, sample_rate=1000, channel_count=2)
        assert not ep.data.flags.writeable
        with pytest.raises(ValueError):
            ep.data[0, 0] = 9.0
        # 副本独立：改源数组不影响已构造的 Epoch
        src[0, 0] = 7.0
        assert ep.data[0, 0] == 1.0


# ---------------------------------------------------------------------------
# P2-10: is_high_confidence 阈值提为常量
# ---------------------------------------------------------------------------

class TestHighConfidenceThreshold:
    def test_threshold_boundary(self):
        from omnibci.domain.classification import (
            Classification, HIGH_CONFIDENCE_THRESHOLD)
        assert HIGH_CONFIDENCE_THRESHOLD == 0.7
        at = Classification(target_id=0, event_id=1, confidence=HIGH_CONFIDENCE_THRESHOLD)
        below = Classification(target_id=0, event_id=1,
                               confidence=HIGH_CONFIDENCE_THRESHOLD - 0.01)
        none = Classification(target_id=0, event_id=1, confidence=None)
        assert at.is_high_confidence is True
        assert below.is_high_confidence is False
        assert none.is_high_confidence is False
