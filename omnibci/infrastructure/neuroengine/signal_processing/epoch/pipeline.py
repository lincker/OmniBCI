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
        latest_samples: np.ndarray,
        event_id: Optional[int] = None,
        baseline: Optional[np.ndarray] = None,
    ) -> Optional[Prediction]:
        """
        Apply filters to the accumulated post-trigger *epoch_data* in order,
        then decode. The window contains samples collected so far, not
        necessarily a completed start-to-end interval.

        *baseline* supplies pre-trigger reference data through the FilterContext.
        Filters choose how to use it while the decoder receives only the window.
        Supplying it does not apply baseline correction by itself.

        *latest_samples* is the most recently appended epoch segment, not all
        samples received since the previous decode. It is passed independently
        of the filtered window, retaining the supplied input values. It is required;
        supply an empty (channels, 0) array when no samples were appended.
        Filters and decoders must not modify the supplied arrays in place.
        Returns the decoder's Prediction, or None if it declines. Either outcome
        may update the decoder's per-epoch state.

        Raises:
            TypeError: if *latest_samples* is omitted or None.
        """
        if latest_samples is None:
            raise TypeError("latest_samples must be an array; use shape (channels, 0) for an empty segment")
        ctx = FilterContext(
            sample_rate=self.sample_rate,
            event_id=event_id,
            baseline=baseline,
        )
        data = epoch_data
        for stage in self.filters:
            data = stage.apply(data, ctx)
        return self.decoder.decode(data, latest_samples)
