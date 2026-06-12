"""
Epoch — Domain Value Object.

Represents a bounded segment of EEG signal with a known trigger identity.
Immutable once constructed; identity is determined by value, not by object id.

Rules encoded here (previously buried in EpochManager / state.py):
  - An epoch must have at least one sample (data.shape[1] > 0).
  - sample_rate and channel_count must be positive.
  - event_id identifies the stimulus that opened this epoch.

Windowing model (generic, paradigm-neutral):
  - ``data`` is one contiguous segment laid out as  [ baseline | window ].
  - ``trigger_offset`` is the number of pre-trigger samples, i.e. the index of
    the trigger within ``data``.  ``baseline`` is everything before it and
    ``window`` everything from it onward (the main analysis segment).
  - ``trigger_offset == 0`` means "no baseline captured" — fully backward
    compatible with epochs that begin at the trigger.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class Epoch:
    """
    A single, time-bounded EEG recording triggered by a stimulus event.

    Attributes:
        data:          EEG signal matrix, shape (channels, samples). Read-only view.
                       Laid out as [ baseline | window ].
        event_id:      Trigger event identifier that opened this epoch.
        sample_rate:   Samples per second.
        channel_count: Number of EEG channels.
        trigger_offset:Pre-trigger sample count = index of the trigger within
                       ``data``. 0 means no baseline was captured.
        created_at:    Wall-clock time of construction (UTC).
    """
    data: np.ndarray          # shape: (channels, samples)
    event_id: int
    sample_rate: int
    channel_count: int
    trigger_offset: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.data.ndim != 2:
            raise ValueError(
                f"Epoch.data must be 2-D (channels × samples), got shape {self.data.shape}"
            )
        if self.data.shape[1] == 0:
            raise ValueError("Epoch must contain at least one sample.")
        if self.sample_rate <= 0:
            raise ValueError(f"sample_rate must be positive, got {self.sample_rate}")
        if self.channel_count <= 0:
            raise ValueError(f"channel_count must be positive, got {self.channel_count}")
        if self.data.shape[0] != self.channel_count:
            raise ValueError(
                f"data has {self.data.shape[0]} rows but channel_count={self.channel_count}"
            )
        if not (0 <= self.trigger_offset < self.data.shape[1]):
            raise ValueError(
                f"trigger_offset must be in [0, {self.data.shape[1]}) "
                f"(at least one window sample required), got {self.trigger_offset}"
            )
        # Make underlying array read-only to enforce value-object immutability
        object.__setattr__(self, "data", self.data)
        self.data.flags.writeable = False

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def duration_samples(self) -> int:
        return self.data.shape[1]

    @property
    def duration_seconds(self) -> float:
        return self.duration_samples / self.sample_rate

    @property
    def baseline(self) -> np.ndarray:
        """Pre-trigger segment (channels × trigger_offset). Empty if none."""
        return self.data[:, : self.trigger_offset]

    @property
    def window(self) -> np.ndarray:
        """Post-trigger segment (channels × …) — the main analysis window."""
        return self.data[:, self.trigger_offset :]

    @property
    def has_baseline(self) -> bool:
        return self.trigger_offset > 0

    # ------------------------------------------------------------------
    # Equality — based on signal content + metadata (not object identity)
    # ------------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Epoch):
            return NotImplemented
        return (
            self.event_id == other.event_id
            and self.sample_rate == other.sample_rate
            and self.channel_count == other.channel_count
            and self.trigger_offset == other.trigger_offset
            and np.array_equal(self.data, other.data)
        )

    def __hash__(self) -> int:
        # Hashable only on metadata; large arrays are not hashed by value.
        return hash((self.event_id, self.sample_rate, self.channel_count, self.data.shape))

    def __repr__(self) -> str:
        return (
            f"Epoch(event_id={self.event_id}, "
            f"shape={self.data.shape}, "
            f"trigger_offset={self.trigger_offset}, "
            f"sample_rate={self.sample_rate}Hz, "
            f"duration={self.duration_seconds:.3f}s)"
        )
