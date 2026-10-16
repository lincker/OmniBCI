"""
ProcessingPipeline — Filter* → Decoder.
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
        event_id: Optional[int] = None,
        baseline: Optional[np.ndarray] = None,
        latest_samples: Optional[np.ndarray] = None,
    ) -> Optional[Prediction]:
        """
        Apply filters to *epoch_data* (the post-trigger window) in order, then
        decode.

        *baseline* supplies pre-trigger reference data through the FilterContext.
        Filters choose how to use it while the decoder receives only the window.
        Supplying it does not apply baseline correction by itself.

        *latest_samples* is the most recently appended epoch segment, not all
        samples received since the previous decode. It is passed independently
        of the filtered window, retaining the supplied input values. When it is
        omitted, the decoder receives the filtered full window for both inputs.
        Returns the decoder's Prediction, or None if it declines.
        """
        ctx = FilterContext(
            sample_rate=self.sample_rate,
            event_id=event_id,
            baseline=baseline,
        )
        data = epoch_data
        for stage in self.filters:
            data = stage.apply(data, ctx)
        return self.decoder.decode(data, latest_samples if latest_samples is not None else data)
