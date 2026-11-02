"""Ordered stream preprocessing before acquisition publishes its chunks."""

from dataclasses import replace
import logging
from typing import Protocol

import numpy as np

from omnibci.domain.signal import SignalChunk


logger = logging.getLogger(__name__)


class ChunkPreprocessor(Protocol):
    """Transform a chunk without mutating its input or changing sample positions."""

    def process(self, chunk: SignalChunk) -> SignalChunk: ...

    def reset(self) -> None: ...


class TriggerEdgePreprocessor:
    """Keep the first sample of each nonzero value run across chunk boundaries."""

    def __init__(self) -> None:
        self._previous_trigger = 0

    def reset(self) -> None:
        self._previous_trigger = 0

    def process(self, chunk: SignalChunk) -> SignalChunk:
        triggers = chunk.triggers
        if triggers.size == 0:
            return chunk

        previous_trigger = self._previous_trigger
        repeated = np.empty(triggers.shape, dtype=bool)
        repeated[0] = triggers[0] == previous_trigger
        repeated[1:] = triggers[1:] == triggers[:-1]
        repeated &= triggers != 0
        self._previous_trigger = triggers[-1]
        result = chunk
        if repeated.any():
            raw_data = chunk.raw_data.copy()
            raw_data[-1, repeated] = 0
            result = replace(chunk, raw_data=raw_data)

        if logger.isEnabledFor(logging.INFO):
            nonzero = int(np.count_nonzero(triggers))
            if nonzero:
                suppressed = int(np.count_nonzero(repeated))
                kept = nonzero - suppressed
                positions = np.flatnonzero(result.triggers)[:8]
                events = [(int(index), result.triggers[index].item()) for index in positions]
                logger.info(
                    "Trigger preprocessing: sequence_id=%s samples=%d input_nonzero=%d "
                    "kept=%d suppressed=%d previous_trigger=%s last_trigger=%s "
                    "kept_events=%s events_truncated=%s",
                    chunk.sequence_id, triggers.size, nonzero, kept, suppressed,
                    previous_trigger, self._previous_trigger, events, kept > len(events),
                )
        return result
