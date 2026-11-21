"""
ProcessingPipeline — Filter* → Decoder.
"""

from typing import List, Optional, Sequence

import numpy as np

from omnibci.domain.ports.algorithm import Decoder, Filter, Prediction
from omnibci.infrastructure.signal_processing.arrays import matrix


class ProcessingPipeline:
    """Ordered filters followed by a terminal decoder."""

    def __init__(
        self,
        decoder: Decoder,
        filters: Optional[Sequence[Filter]] = None,
    ) -> None:
        self.decoder = decoder
        self.filters: List[Filter] = list(filters or [])

    def run(
        self,
        epoch_data: np.ndarray,
        *,
        latest_raw_segment: np.ndarray,
        pre_trigger_data: Optional[np.ndarray] = None,
    ) -> Optional[Prediction]:
        """Transform a whole window, retaining the original raw segment and reference."""
        data = matrix(epoch_data, "Epoch window")
        latest = matrix(latest_raw_segment, "latest_raw_segment", channels=data.shape[0])
        reference = (matrix(pre_trigger_data, "pre_trigger_data", channels=data.shape[0])
                     if pre_trigger_data is not None else None)
        if data.shape[1] == 0:
            return None
        shape = data.shape
        for index, stage in enumerate(self.filters):
            label = f"Filter {index} ({type(stage).__module__}.{type(stage).__qualname__})"
            try:
                data = matrix(stage.apply(data, pre_trigger_data=reference), label, shape=shape)
            except Exception as exc:
                exc.add_note(label)
                raise
        return self.decoder.decode(data, latest_raw_segment=latest)
