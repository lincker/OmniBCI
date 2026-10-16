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
    Pure transform: no termination decision. Treat input arrays, including
    context reference data, as borrowed and read-only; do not modify them in place.
    """

    @abstractmethod
    def apply(self, data: npt.NDArray, ctx: FilterContext) -> npt.NDArray:
        """Transform *data* (channels × samples) and return the result."""


@dataclass(frozen=True)
class Prediction:
    """A decoder's output for one epoch window (immutable).

    target_id must be a Python int or NumPy integer scalar, excluding booleans.
    Signal Processing validates this before conversion; floats (even 1.0),
    strings, and arrays are rejected. Classification enforces nonnegative
    targets and confidence in [0, 1] when converting a valid Prediction.
    """
    target_id: int
    confidence: Optional[float] = None


class Decoder(ABC):
    """
    Turns an accumulated, filtered post-trigger window into a Prediction.

    Contract:
      - should_decode(window) — cheap gate on the accumulated window before
        epoch filters run. Normal decoding checks this gate; a closing
        epoch boundary can bypass it.
      - decode(window, latest_samples) — produce a Prediction, or None if not ready /
        not confident. Either outcome may update the decoder's per-epoch state.

    Treat input arrays as borrowed and read-only; copy them if mutable working
    storage is needed. Stateful decoders must isolate working state per epoch
    through for_new_epoch(). This interface does not guarantee delivery of every
    sample through latest_samples.
    """

    @abstractmethod
    def should_decode(self, window: npt.NDArray) -> bool:
        """
        Cheap precondition gate — is it worth attempting a decode on *window* now?
        Receives the accumulated post-trigger window before epoch filters run,
        without pre-trigger samples or a trigger row. Override with a *cheap*
        check (e.g. enough samples / repetitions). True permits an attempt; it
        does not guarantee that decode() will return a Prediction.
        """

    @abstractmethod
    def decode(self, window: npt.NDArray, latest_samples: npt.NDArray) -> Optional[Prediction]:
        """
        Decode → Prediction, or None if the decoder declines (not ready / not
        confident).

        Args:
            window: the accumulated post-trigger epoch window after configured
                    filters (channels × samples), from the start sample through
                    the current collection position, excluding a closing boundary
                    sample. It need not extend to an end trigger. Whole-window
                    decoders (SSVEP CCA, ERP averaging) use this input and choose
                    their own analysis segment.
            latest_samples: the most recently appended epoch segment before
                    epoch filters. Upstream channel mapping and re-referencing
                    already apply to both inputs. This is not all samples received
                    since the previous decode. It must be supplied explicitly;
                    an empty segment has shape (channels, 0), never None.

        Neither input contains pre-trigger reference samples or a trigger row.
        Either input may be ignored depending on the decoder's style. A Prediction
        completes the current collection. None means no prediction this time, not
        that the input was unprocessed or decoder state was unchanged.
        """

    def for_new_epoch(self) -> "Decoder":
        """
        Return the decoder instance to use for a freshly opened epoch.

        The engine creates one EpochProcessor per epoch and calls this to obtain
        that epoch's decoder. The default returns ``self`` — correct for stateless
        decoders (which recompute from the accumulated window each call), so one
        shared instance can be reused.

        A decoder that keeps mutable per-epoch state across calls
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
