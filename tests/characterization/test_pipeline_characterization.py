"""
Characterization / contract tests — ProcessingPipeline（Filter* → Decoder.decode）.

锁定解码契约：
  - 无 filter 时 pipeline.run 等价于直接 decoder.decode(epoch_data)。
  - filters 按顺序作用于 epoch_data，再交给 decoder。
  - 返回 Optional[Prediction]（decoder 不出结果 → None）。
  - decode 已无 forced 形参：pipeline 的 forced 只传给 FilterContext，不影响 decode。

只依赖 numpy；纯 numpy 环境用独立 runner 可跑。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from omnibci.domain.ports.algorithm import Decoder, Filter, FilterContext, Prediction
from omnibci.infrastructure.neuroengine.processing.epoch.pipeline import ProcessingPipeline


class _SpyDecoder(Decoder):
    def __init__(self, ready=True, target=5):
        super().__init__()
        self._ready = ready
        self._target = target
        self.seen_data = None

    def should_decode(self, data) -> bool:
        return self._ready

    def decode(self, data, latest) -> Optional[Prediction]:
        # Two-arg port contract: decode(window, latest). The pipeline defaults
        # latest to the full window when the caller supplies no delta.
        self.seen_data = data
        self.seen_latest = latest
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
        pred = pipe.run(epoch, forced=False, event_id=4)
        assert pred == Prediction(target_id=9, confidence=0.9)
        assert np.array_equal(dec.seen_data, epoch)   # decoder saw the unfiltered data

    def test_decline_returns_none(self):
        dec = _SpyDecoder(ready=False)
        pipe = ProcessingPipeline(decoder=dec)
        assert pipe.run(np.ones((2, 4)), forced=False) is None

    def test_forced_does_not_reach_decoder(self):
        # decode lost its forced param: a not-ready decoder still declines even
        # when the pipeline is asked with forced=True (forced only feeds filters).
        dec = _SpyDecoder(ready=False)
        pipe = ProcessingPipeline(decoder=dec)
        assert pipe.run(np.ones((2, 4)), forced=True) is None


class TestPipelineWithFilters:
    def test_filters_apply_in_order_before_decode(self):
        dec = _SpyDecoder()
        pipe = ProcessingPipeline(decoder=dec, filters=[_AddFilter(1), _AddFilter(10)])
        pipe.run(np.zeros((2, 3)), forced=False, event_id=7)
        assert np.all(dec.seen_data == 11)   # 0 + 1 + 10

    def test_filter_receives_context_including_forced(self):
        dec = _SpyDecoder()
        f = _AddFilter(0)
        pipe = ProcessingPipeline(decoder=dec, filters=[f], sample_rate=512)
        pipe.run(np.ones((2, 2)), forced=True, event_id=42)
        assert f.seen_ctx.sample_rate == 512
        assert f.seen_ctx.forced is True       # forced still reaches filters
        assert f.seen_ctx.event_id == 42


if __name__ == "__main__":  # pragma: no cover
    import traceback
    passed = failed = 0
    for cls in (TestPipelineNoFilters, TestPipelineWithFilters):
        inst = cls()
        for name in sorted(n for n in dir(inst) if n.startswith("test_")):
            try:
                getattr(inst, name)(); passed += 1; print(f"PASS {cls.__name__}.{name}")
            except Exception:
                failed += 1; print(f"FAIL {cls.__name__}.{name}"); traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
