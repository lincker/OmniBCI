"""
Classification — Domain Value Object.

The result produced by a decoder after analysing one Epoch.
Replaces the bare `int` that was previously emitted via _emit_result().

Business rules encoded here:
  - target_id must be non-negative.
  - confidence is in [0.0, 1.0]; None means the decoder did not provide it.
  - forced=True means recognition was triggered early (end-event or unexpected start),
    which callers may treat differently (e.g. lower priority).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

#: 判定"高置信"的标准阈值（confidence ≥ 此值）。原先硬编码在
#: is_high_confidence 内（P2-10 提为常量）。
HIGH_CONFIDENCE_THRESHOLD = 0.7


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
        produced_at: UTC timestamp when this result was created.
    """
    target_id: int
    event_id: int
    confidence: Optional[float] = None
    forced: bool = False
    produced_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.target_id < 0:
            raise ValueError(f"target_id must be ≥ 0, got {self.target_id}")
        if self.confidence is not None and not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0, 1], got {self.confidence}"
            )

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def is_high_confidence(self) -> bool:
        """True when confidence is available and above the standard threshold."""
        return self.confidence is not None and self.confidence >= HIGH_CONFIDENCE_THRESHOLD

    def __repr__(self) -> str:
        conf = f"{self.confidence:.2f}" if self.confidence is not None else "n/a"
        return (
            f"Classification(target={self.target_id}, "
            f"event={self.event_id}, "
            f"confidence={conf}, "
            f"forced={self.forced})"
        )
