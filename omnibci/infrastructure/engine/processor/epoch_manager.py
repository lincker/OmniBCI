"""
EpochManager — Infrastructure helper for the processor.

Responsibility:
  - Collect raw signal chunks into an epoch buffer.
  - Detect epoch boundaries via EpochDetectorBase.
  - Construct an immutable Epoch value-object once the buffer is ready.
  - Run the decoder and wrap the result as a Classification value-object.

What changed from the original:
  - run_pipeline() now returns (bool, Optional[Classification]) instead of
    (bool, int), so callers hold a rich domain object, not a bare int.
  - build_epoch() explicitly constructs an Epoch VO before decoding.
  - The decoder's legacy (bool, int) return is wrapped here; existing decoder
    plugins do NOT need to change their signatures.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

import numpy as np

from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.domain.ports.algorithm import DecoderBase, EpochDetectorBase
from omnibci.shared.signal import SignalChunk

logger = logging.getLogger(__name__)


class EpochManager:
    """
    Stateful helper owned by SignalProcessor.

    Lifecycle per trial:
        initialize_epoch()            — buffer reset, first samples stored
        append_chunk() / append_until_index() — stream more samples
        build_epoch()                 — construct immutable Epoch VO
        run_pipeline()                — decode Epoch → Classification
        clear()                       — prepare for next trial
    """

    def __init__(
        self,
        decoder: DecoderBase,
        detector: EpochDetectorBase,
        sample_rate: int = 1000,
    ) -> None:
        self.decoder = decoder
        self.detector = detector
        self.sample_rate = sample_rate

        self._epoch_data: np.ndarray = np.empty((0, 0))
        self._current_chunk_data: Optional[np.ndarray] = None
        self._current_event_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Boundary detection (delegates to EpochDetectorBase)
    # ------------------------------------------------------------------

    def detect_start(self, chunk: SignalChunk) -> Tuple[int, Optional[int]]:
        return self.detector.detect_start(chunk)

    def detect_end(self, chunk: SignalChunk) -> Tuple[int, Optional[int]]:
        return self.detector.detect_end(chunk)

    # ------------------------------------------------------------------
    # Buffer management
    # ------------------------------------------------------------------

    def initialize_epoch(self, chunk: SignalChunk, event_id: int) -> None:
        """
        Start a new epoch buffer from the trigger point within *chunk*.

        Raises:
            ValueError: if no start event is present in *chunk*.
        """
        start_idx, _ = self.detect_start(chunk)
        if start_idx < 0:
            raise ValueError("Cannot initialize epoch: no start event found in chunk")

        self._epoch_data = chunk.data[:, start_idx:]
        self._current_event_id = event_id
        self._current_chunk_data = self._epoch_data
        logger.info(
            "Epoch initialised: event_id=%d, initial_shape=%s",
            event_id, self._epoch_data.shape,
        )

    def append_chunk(self, chunk: SignalChunk) -> None:
        """Append all samples in *chunk* to the epoch buffer."""
        self._current_chunk_data = chunk.data
        self._epoch_data = np.append(self._epoch_data, chunk.data, axis=1)

    def append_until_index(self, chunk: SignalChunk, index: int) -> None:
        """
        Append samples up to (not including) *index*.

        Raises:
            ValueError: if *index* is negative.
        """
        if index < 0:
            raise ValueError(f"Invalid slice index: {index}")
        data_slice = chunk.data[:, :index]
        self._current_chunk_data = data_slice
        self._epoch_data = np.append(self._epoch_data, data_slice, axis=1)

    def clear(self) -> None:
        """Reset the buffer; call after a trial completes."""
        self._epoch_data = np.empty((0, 0))
        self._current_chunk_data = None
        self._current_event_id = None

    # ------------------------------------------------------------------
    # Domain-object construction
    # ------------------------------------------------------------------

    def build_epoch(self) -> Epoch:
        """
        Construct an immutable Epoch value-object from the current buffer.

        Raises:
            RuntimeError: if no epoch is being collected.
            ValueError: propagated from Epoch.__post_init__ on bad data.
        """
        if self._current_event_id is None:
            raise RuntimeError("No active epoch; call initialize_epoch() first")
        if self._epoch_data.size == 0:
            raise RuntimeError("Epoch buffer is empty; cannot build Epoch VO")

        channel_count = self._epoch_data.shape[0]
        return Epoch(
            data=self._epoch_data.copy(),   # defensive copy before freezing
            event_id=self._current_event_id,
            sample_rate=self.sample_rate,
            channel_count=channel_count,
        )

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    def run_pipeline(
        self,
        force_recognize: bool = False,
    ) -> Tuple[bool, Optional[Classification]]:
        """
        Run the decoder on the current epoch buffer.

        Args:
            force_recognize: Skip the decoder's own completion check and
                             force a prediction immediately.

        Returns:
            (is_complete, classification)
            - is_complete=False  → the decoder decided not to predict yet.
            - is_complete=True   → classification is a Classification VO.
        """
        if self._epoch_data.size == 0 or self._current_event_id is None:
            return False, None

        # DecoderBase.analyse() still returns legacy (bool, int).
        # We wrap the result here so no existing plugin needs modification.
        is_complete, raw_target = self.decoder.analyse(
            self._epoch_data,
            self._current_chunk_data,
            force_recognize,
        )

        if not is_complete:
            return False, None

        classification = Classification(
            target_id=int(raw_target),
            event_id=self._current_event_id,
            forced=force_recognize,
        )
        logger.info(
            "Decoder produced: %s (force=%s)", classification, force_recognize
        )
        return True, classification
