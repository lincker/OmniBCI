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
