"""
Algorithm domain ports — the decoding pipeline's plugin contracts.

  Filter        — one ordered preprocessing stage (pure transform).
  Decoder       — turns a filtered epoch window into a Prediction.
  EpochDetector — finds epoch start/end markers in a signal chunk.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Optional

from numpy import typing as npt

from omnibci.domain.classification import _normalize_confidence, _normalize_target_id
from omnibci.domain.signal_info import integer


@dataclass(frozen=True, slots=True, kw_only=True)
class EpochBoundary:
    """One ordered sample boundary; only a start carries an event label."""
    offset: int
    kind: Literal["start", "end"]
    event_id: int | None

    def __post_init__(self) -> None:
        integer(self.offset, "boundary offset")
        if self.kind == "start":
            integer(self.event_id, "start event_id", 1)
        elif self.kind != "end" or self.event_id is not None:
            raise ValueError("Boundary must be start with an event ID or end with event_id=None")


class Filter(ABC):
    """Transform a whole window, preserving shape, channel order, rate and units.

    Inputs are borrowed read-only arrays. Copy anything retained or mutated.
    Repeated calls contain accumulated windows, not incremental sample delivery.
    """

    @abstractmethod
    def apply(self, data: npt.NDArray, *, pre_trigger_data: npt.NDArray | None) -> npt.NDArray:
        """Use the original pre-trigger reference if needed; None/zero width means absent."""


@dataclass(frozen=True)
class Prediction:
    """A decoder's output for one epoch window (immutable).

    Nonnegative Python/NumPy integer targets are normalized to Python int.
    Floats (even 1.0), booleans, strings, and arrays are rejected. Confidence
    is absent or a finite non-boolean real number in [0, 1], stored as float.
    """
    target_id: int
    confidence: Optional[float] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _normalize_target_id(self.target_id))
        object.__setattr__(self, "confidence", _normalize_confidence(self.confidence))


class Decoder(ABC):
    """One prepared model, with explicitly reset working state for one active epoch.

    Calls are serialized by the host. Arrays are borrowed and read-only; copy
    anything retained or mutated. Event labels and runtime identity stay in the host.
    """

    @abstractmethod
    def setup(self) -> None:
        """Prepare reusable model resources once for this binding."""

    @abstractmethod
    def begin_epoch(self) -> None:
        """Reset working state on every start, retaining reusable model resources."""

    @abstractmethod
    def should_decode(self, raw_window: npt.NDArray) -> bool:
        """Return a boolean scalar for the raw accumulated window, without event row."""

    @abstractmethod
    def decode(self, filtered_window: npt.NDArray, *, latest_raw_segment: npt.NDArray) -> Optional[Prediction]:
        """Decode an accumulated filtered window; None means no prediction this time.

        latest_raw_segment is only the most recently appended mapped segment,
        before epoch filters, not all samples since the preceding decode. It can
        be an empty (channels, 0) array. Neither input contains pre-trigger samples
        or an event row. Closing markers bypass the gate: short windows may occur.
        A Prediction completes collection. Either outcome may update working state.
        """

    @abstractmethod
    def teardown(self) -> None:
        """Release resources, including after partially failed setup."""


class EpochDetector(ABC):
    """Detect all boundaries in one mapped matrix, including its normalized event row."""

    @abstractmethod
    def detect(self, raw_data: npt.NDArray) -> tuple[EpochBoundary, ...]:
        """Return at most 2*N boundaries with offsets in [0, N).

        Order by offset, end before start on ties, without duplicate kinds at an
        offset. The host includes opening samples and excludes closing samples.
        Borrow the input read-only; input replacement creates a fresh detector.
        """
