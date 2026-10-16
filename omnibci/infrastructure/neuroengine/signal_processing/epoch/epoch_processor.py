"""
EpochProcessor — Infrastructure helper for the processor.

Responsibility:
  - Epoching: collect raw signal chunks into an epoch buffer (boundary indices are
    located by SignalProcessor's EpochDetector and passed in).
  - Construct an immutable Epoch value-object once the buffer is ready.
  - Run the decoder and wrap the result as a Classification value-object.
"""

import logging
from typing import List, Optional, Tuple

import numpy as np

from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.domain.ports.algorithm import Decoder, Filter
from omnibci.infrastructure.neuroengine.signal_processing.epoch.pipeline import ProcessingPipeline
from omnibci.domain.signal import SignalChunk

logger = logging.getLogger(__name__)


class EpochProcessor:
    """
    Stateful helper owned by SignalProcessor.

    One instance per epoch (created fresh on each detected start, dropped on
    completion). Lifecycle:
        initialize_epoch()            — first samples stored from start_idx
        append_chunk() / append_until_index() — stream more samples
        build_epoch()                 — construct immutable Epoch VO
        _run_pipeline()               — decode window → Classification
    """

    def __init__(
        self,
        decoder: Decoder,
        sample_rate: int = 1000,
        filters: Optional[List[Filter]] = None,
        config_revision: str | None = None,
    ) -> None:
        self.decoder = decoder
        self.sample_rate = sample_rate
        self.config_revision = config_revision
        # Ordered preprocessing followed by the terminal decoder.
        self.pipeline = ProcessingPipeline(
            decoder=decoder, filters=filters, sample_rate=sample_rate
        )

        # Post-trigger samples (the analysis window). The decoder sees ONLY this,
        # exactly as before — pre-trigger data is kept separate so decoder inputs are
        # unchanged.
        self._epoch_data: np.ndarray = np.empty((0, 0))
        # Pre-trigger samples captured at epoch open (channels × trigger_offset).
        # Empty when pre-trigger capture is disabled or no history is available.
        self._pre_trigger_data: np.ndarray = np.empty((0, 0))
        self._current_chunk_data: Optional[np.ndarray] = None
        self._event_id: Optional[int] = None
        self._pending_decode = False

    # ------------------------------------------------------------------
    # Buffer management
    # ------------------------------------------------------------------

    def initialize_epoch(
        self,
        chunk: SignalChunk,
        start_idx: int,
        event_id: int,
        pre_trigger_data: Optional[np.ndarray] = None,
    ) -> None:
        """
        Start a new epoch buffer from *start_idx* within *chunk*.

        *start_idx* is the start-marker index located by the caller's
        EpochDetector (SignalProcessor), so the epoch processor stays free of
        trigger-channel concerns.

        *pre_trigger_data* (channels × n) is the optional reference data supplied by
        the caller's PreTriggerBuffer. It is stored separately from the analysis
        window: the decoder still sees only post-trigger samples, while
        ``build_epoch`` stitches pre-trigger data + window into the Epoch VO.

        Raises:
            ValueError: if *start_idx* is negative.
        """
        if start_idx < 0:
            raise ValueError(f"Cannot initialize epoch: invalid start index {start_idx}")

        self._epoch_data = chunk.data[:, start_idx:]
        # Default to empty pre-trigger data with the correct channel count.
        if pre_trigger_data is None:
            pre_trigger_data = chunk.data[:, :0]
        self._pre_trigger_data = pre_trigger_data
        self._event_id = event_id
        self._current_chunk_data = self._epoch_data
        self._pending_decode = True
        logger.info(
            "Epoch initialised: event_id=%d, window_shape=%s, pre_trigger_samples=%d",
            event_id, self._epoch_data.shape, self._pre_trigger_data.shape[1],
        )

    def append_chunk(self, chunk: SignalChunk) -> None:
        """Append all samples in *chunk* to the epoch buffer."""
        self._current_chunk_data = chunk.data
        self._epoch_data = np.append(self._epoch_data, chunk.data, axis=1)
        self._pending_decode = True

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
        self._pending_decode = True

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
        if self._event_id is None:
            raise RuntimeError("No active epoch; call initialize_epoch() first")
        if self._epoch_data.size == 0:
            raise RuntimeError("Epoch buffer is empty; cannot build Epoch VO")

        channel_count = self._epoch_data.shape[0]
        # Stitch [ pre_trigger_data | window ] into one contiguous segment; the
        # trigger sits at index trigger_offset == pre-trigger sample count.
        trigger_offset = self._pre_trigger_data.shape[1]
        if trigger_offset:
            full = np.concatenate([self._pre_trigger_data, self._epoch_data], axis=1)
        else:
            full = self._epoch_data
        return Epoch(
            # Epoch copies and freezes its array; no extra defensive copy is needed.
            data=full,
            event_id=self._event_id,
            sample_rate=self.sample_rate,
            channel_count=channel_count,
            trigger_offset=trigger_offset,
        )

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    def should_decode(self) -> bool:
        """
        Should the pipeline run this tick? True only when an epoch is active AND
        the decoder's cheap precondition gate (decoder.should_decode) is met.
        """
        if self._epoch_data.size == 0 or self._event_id is None:
            return False
        return self.decoder.should_decode(self._epoch_data)

    def process_chunk(self, chunk: SignalChunk) -> Tuple[bool, Optional[Classification]]:
        """
        Ingest one chunk, then decode **only if** should_decode() is satisfied.

        Returns (is_complete, classification). (False, None) means "keep
        collecting" — either the gate is not met, or the decoder declined.
        """
        self.append_chunk(chunk)
        if not self.should_decode():
            return False, None
        return self._run_pipeline()

    def force_decode_at_boundary(self) -> Tuple[bool, Optional[Classification]]:
        """Attempt decoding at a closing boundary, bypassing the readiness gate."""
        return self._run_pipeline(forced=True)

    def decode_if_ready(self) -> Tuple[bool, Optional[Classification]]:
        """Decode newly buffered data only when the decoder's readiness gate is met."""
        if not self._pending_decode or not self.should_decode():
            return False, None
        return self._run_pipeline()

    def _run_pipeline(self, forced: bool = False) -> Tuple[bool, Optional[Classification]]:
        """
        Decode the current buffer. Used by marker-forced paths
        (end / unexpected-start), which bypass the should_decode() gate.
        """
        if self._epoch_data.size == 0 or self._event_id is None:
            return False, None
        self._pending_decode = False
        prediction = self.pipeline.run(
            self._epoch_data,
            event_id=self._event_id,
            baseline=self._pre_trigger_data,
            latest_samples=self._current_chunk_data,  # most recently appended segment
        )
        if prediction is None:
            return False, None
        classification = Classification(
            target_id=int(prediction.target_id),
            event_id=self._event_id,
            confidence=prediction.confidence,
            forced=forced,
        )
        logger.info("Decoder produced: %s (forced=%s)", classification, forced)
        return True, classification
