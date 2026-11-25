"""
Classification — Domain Value Object.

The result produced by a decoder after analysing one epoch.

Business rules encoded here:
  - target_id must be non-negative.
  - confidence is in [0.0, 1.0]; None means the decoder did not provide it.
  - forced=True means recognition was triggered early (end-event or unexpected start),
    which callers may treat differently (e.g. lower priority).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from numbers import Integral, Real

from omnibci.domain.signal_info import integer


def _normalize_target_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError("target_id must be an integer scalar")
    if value < 0:
        raise ValueError("target_id must be nonnegative")
    return int(value)


def _normalize_confidence(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError("confidence must be a real number or None")
    if not 0 <= value <= 1 or not isfinite(value):
        raise ValueError("confidence must be finite and in [0, 1]")
    return float(value)


@dataclass(frozen=True)
class Classification:
    """
    Immutable result of one BCI recognition trial.

    Attributes:
        target_id:   Index of the recognised stimulus target (≥ 0).
        event_id:    Trigger event that opened the corresponding epoch.
        confidence:  Decoder confidence score in [0, 1], or None if unavailable.
        forced:      True when recognition was triggered by an end/reset event
                     rather than by the decoder's own completion criterion.
        produced_at: Timezone-aware creation timestamp; defaults to UTC.
    """
    target_id: int
    event_id: int
    confidence: float | None = None
    forced: bool = False
    produced_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _normalize_target_id(self.target_id))
        object.__setattr__(self, "confidence", _normalize_confidence(self.confidence))
        integer(self.event_id, "event_id", 1)
        if type(self.forced) is not bool:
            raise TypeError("forced must be a boolean")
        if not isinstance(self.produced_at, datetime):
            raise TypeError("produced_at must be a datetime")
        if self.produced_at.tzinfo is None or self.produced_at.utcoffset() is None:
            raise ValueError("produced_at must include a timezone offset")

    def __repr__(self) -> str:
        conf = f"{self.confidence:.2f}" if self.confidence is not None else "n/a"
        return (
            f"Classification(target={self.target_id}, "
            f"event={self.event_id}, "
            f"confidence={conf}, "
            f"forced={self.forced})"
        )
