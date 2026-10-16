"""
Algorithm domain ports — the decoding pipeline's plugin contracts.

  Filter        — one ordered preprocessing stage (pure transform).
  Decoder       — turns a filtered epoch window into a Prediction.
  EpochDetector — finds epoch start/end markers in a signal chunk.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

from numpy import typing as npt

from omnibci.domain.signal import SignalChunk


@dataclass(frozen=True)
class FilterContext:
    """Per-run metadata handed to each Filter (immutable)."""
    sample_rate: int
    event_id: Optional[int] = None
    # Pre-trigger reference data for this epoch (channels × n), or None when
    # not supplied. Capturing it does not apply baseline correction; each Filter
    # chooses how to use these samples.
    baseline: Optional[npt.NDArray] = None


class Filter(ABC):
    """
    A single ordered preprocessing stage in a ProcessingPipeline.

    Takes the current epoch window, transforms it (band-pass, baseline correction,
    downsample, channel selection, …) and returns it for the next stage.
    Pure transform: no termination decision.
    """

    @abstractmethod
    def apply(self, data: npt.NDArray, ctx: FilterContext) -> npt.NDArray:
        """Transform *data* (channels × samples) and return the result."""


@dataclass(frozen=True)
class Prediction:
    """A decoder's output for one epoch window (immutable)."""
    target_id: int
    confidence: Optional[float] = None


class Decoder(ABC):
    """
    Turns a (filtered) epoch window into a recognized target.

    Contract (general — fits discrete SSVEP/P300/MI and continuous decoders):
      - should_decode(data) — cheap gate: are the conditions ready
        for *data* yet? Normal decoding checks this gate; decoding at a closing
        epoch boundary can bypass it.
      - decode(window, latest_samples) — produce a Prediction, or None if not ready /
        not confident. Pure function of its inputs.
    """

    @abstractmethod
    def should_decode(self, window: npt.NDArray) -> bool:
        """
        Cheap precondition gate — is it worth attempting a decode on *window* now?
        Looks only at the accumulated epoch window. Override with a *cheap* check
        (e.g. enough samples / repetitions) so the engine skips the pipeline until
        the epoch is ready.
        """

    @abstractmethod
    def decode(self, window: npt.NDArray, latest_samples: npt.NDArray) -> Optional[Prediction]:
        """
        Decode → Prediction, or None if the decoder declines (not ready / not
        confident).

        Args:
            window: the accumulated post-trigger epoch window after configured
                    filters (channels × samples).
                    Whole-window decoders (SSVEP CCA, ERP averaging) use this.
            latest_samples: the most recently appended epoch segment, supplied
                    independently of filtering. It is not all samples received
                    since the previous decode. If the pipeline caller omits it,
                    it defaults to the filtered full window.

        Either input may be ignored depending on the decoder's style.
        """

    def for_new_epoch(self) -> "Decoder":
        """
        Return the decoder instance to use for a freshly opened epoch.

        The engine creates one EpochProcessor per epoch and calls this to obtain
        that epoch's decoder. The default returns ``self`` — correct for stateless
        decoders (which recompute from the full epoch buffer each call), so one
        shared instance is reused with zero cost and full concurrency safety.

        A decoder that keeps mutable per-epoch state across ``should_decode`` calls
        must override this to return a **fresh** instance that shares the heavy,
        immutable model (templates/weights) but has its own working state. This
        keeps per-epoch state isolated (no leakage between trials, safe for
        concurrent/overlapping epochs) without re-paying model construction.
        """
        return self


class EpochDetector(ABC):
    """Detect epoch start / end markers within a signal chunk."""

    @abstractmethod
    def detect_start(self, chunk: SignalChunk) -> Tuple[int, int]:
        """Return (index, event_id) of the first start marker in *chunk*,
        or (-1, 0) if none is found."""

    @abstractmethod
    def detect_end(self, chunk: SignalChunk) -> Tuple[int, int]:
        """Return (index, event_id) of the first end marker in *chunk*,
        or (-1, 0) if none is found."""
