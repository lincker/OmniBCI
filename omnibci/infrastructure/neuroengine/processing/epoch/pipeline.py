"""
ProcessingPipeline — 有序 Filter* → Decoder 的处理流水线。

把"就绪的 epoch 窗口"依次过一串 Filter（带通、基线、降采样…），再交给 Decoder.decode，
回传 `Optional[Prediction]`（None = decoder 暂不出结果）。是否**该**跑流水线由上游
EpochProcessor 经 `decoder.should_decode(...)` 门控；强制路径（marker 收尾）传 forced=True。

filters 为空时 pipeline.run 等价于直接 `decoder.decode(epoch_data, forced=...)`。
"""

from typing import List, Optional, Sequence

import numpy as np

from omnibci.domain.ports.algorithm import Decoder, Filter, FilterContext, Prediction


class ProcessingPipeline:
    """Ordered filters followed by a terminal decoder."""

    def __init__(
        self,
        decoder: Decoder,
        filters: Optional[Sequence[Filter]] = None,
        sample_rate: int = 1000,
    ) -> None:
        self.decoder = decoder
        self.filters: List[Filter] = list(filters or [])
        self.sample_rate = sample_rate

    def run(
        self,
        epoch_data: np.ndarray,
        *,
        forced: bool,
        event_id: Optional[int] = None,
        baseline: Optional[np.ndarray] = None,
        latest: Optional[np.ndarray] = None,
    ) -> Optional[Prediction]:
        """
        Apply filters to *epoch_data* (the post-trigger window) in order, then
        decode.

        *baseline* (the pre-trigger segment) travels in the FilterContext, not in
        the main data stream — so a baseline-correction Filter can use it while
        the decoder still receives only the window.

        *latest* is the most-recently appended chunk (the delta) required by the
        Decoder port's two-argument contract ``decode(window, latest)``;
        incremental decoders consume it, whole-window decoders ignore it. When
        the caller has no delta (e.g. a marker-forced decode) it defaults to the
        full window. Returns the decoder's Prediction, or None if it declines.
        """
        ctx = FilterContext(
            sample_rate=self.sample_rate,
            forced=forced,
            event_id=event_id,
            baseline=baseline,
        )
        data = epoch_data
        for stage in self.filters:
            data = stage.apply(data, ctx)
        return self.decoder.decode(data, latest if latest is not None else data)
