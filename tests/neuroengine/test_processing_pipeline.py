"""Contract tests for ProcessingPipeline filter and decoder composition."""
from __future__ import annotations

from typing import Optional

import numpy as np

from omnibci.domain.ports.algorithm import Decoder, Filter, FilterContext, Prediction
from omnibci.infrastructure.neuroengine.signal_processing.epoch.pipeline import ProcessingPipeline


class _SpyDecoder(Decoder):
    def __init__(self, ready=True, target=5):
        super().__init__()
        self._ready = ready
        self._target = target
        self.seen_data = None

    def should_decode(self, data) -> bool:
        return self._ready

    def decode(self, data, latest_samples) -> Optional[Prediction]:
        # Two-arg port contract: decode(window, latest_samples). The pipeline defaults
        # latest_samples to the filtered full window when omitted.
        self.seen_data = data
        self.seen_latest_samples = latest_samples
        if self._ready:
            return Prediction(target_id=self._target, confidence=0.9)
        return None


class _AddFilter(Filter):
    """Adds a constant; records the ctx it was handed."""
    def __init__(self, k):
        super().__init__()
        self._k = k
        self.seen_ctx = None

    def apply(self, data, ctx: FilterContext):
        self.seen_ctx = ctx
        return data + self._k


class TestPipelineNoFilters:
    def test_empty_pipeline_equals_direct_decode(self):
        dec = _SpyDecoder(ready=True, target=9)
        pipe = ProcessingPipeline(decoder=dec, filters=None, sample_rate=250)
        epoch = np.ones((3, 5))
        pred = pipe.run(epoch, event_id=4)
        assert pred == Prediction(target_id=9, confidence=0.9)
        assert np.array_equal(dec.seen_data, epoch)   # decoder saw the unfiltered data

    def test_decline_returns_none(self):
        dec = _SpyDecoder(ready=False)
        pipe = ProcessingPipeline(decoder=dec)
        assert pipe.run(np.ones((2, 4))) is None


class TestPipelineWithFilters:
    def test_filters_apply_in_order_before_decode(self):
        dec = _SpyDecoder()
        pipe = ProcessingPipeline(decoder=dec, filters=[_AddFilter(1), _AddFilter(10)])
        pipe.run(np.zeros((2, 3)), event_id=7)
        assert np.all(dec.seen_data == 11)   # 0 + 1 + 10
        assert dec.seen_latest_samples is dec.seen_data

    def test_supplied_latest_samples_keep_input_values(self):
        dec = _SpyDecoder()
        pipe = ProcessingPipeline(decoder=dec, filters=[_AddFilter(10)])
        latest_samples = np.ones((2, 1))
        pipe.run(np.zeros((2, 3)), latest_samples=latest_samples)
        assert np.all(dec.seen_data == 10)
        assert dec.seen_latest_samples is latest_samples
        np.testing.assert_array_equal(dec.seen_latest_samples, np.ones((2, 1)))

    def test_filter_receives_context_metadata(self):
        dec = _SpyDecoder()
        f = _AddFilter(0)
        pipe = ProcessingPipeline(decoder=dec, filters=[f], sample_rate=512)
        pipe.run(np.ones((2, 2)), event_id=42)
        assert f.seen_ctx.sample_rate == 512
        assert f.seen_ctx.event_id == 42
        assert f.seen_ctx.baseline is None
