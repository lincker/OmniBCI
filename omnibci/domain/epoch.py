"""
Epoch — Domain Value Object.

Represents a bounded segment of EEG signal with a known trigger identity.
Immutable once constructed; identity is determined by value, not by object id.

Rules encoded here (previously buried in EpochManager / state.py):
  - An epoch must have at least one sample (data.shape[1] > 0).
  - sample_rate and channel_count must be positive.
  - event_id identifies the stimulus that opened this epoch.

Windowing model (generic, paradigm-neutral):
  - ``data`` is one contiguous segment laid out as [ pre_trigger_data | window ].
  - ``trigger_offset`` is the number of pre-trigger samples, i.e. the index of
    the trigger within ``data``. ``pre_trigger_data`` is everything before it and
    ``window`` everything from it onward (the main analysis segment).
  - Capturing pre-trigger data does not apply baseline correction. A filter
    chooses how to use these reference samples.
  - ``trigger_offset == 0`` means "no pre-trigger data captured".
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class Epoch:
    """
    A single, time-bounded EEG recording triggered by a stimulus event.

    Attributes:
        data:          EEG signal matrix, shape (channels, samples). Read-only view.
                       Laid out as [ pre_trigger_data | window ].
        event_id:      Trigger event identifier that opened this epoch.
        sample_rate:   Samples per second.
        channel_count: Number of EEG channels.
        trigger_offset:Pre-trigger sample count = index of the trigger within
                       ``data``. 0 means no pre-trigger data was captured.
    """
    data: np.ndarray          # shape: (channels, samples)
    event_id: int
    sample_rate: int
    channel_count: int
    trigger_offset: int = 0

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
        # Freeze as an immutable value object: hold a read-only copy of our
        # own, so the caller's array is never mutated in place.
        frozen = np.array(self.data, copy=True)
        frozen.flags.writeable = False
        object.__setattr__(self, "data", frozen)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def duration_samples(self) -> int:
        """Total captured samples, including pre-trigger data."""
        return self.data.shape[1]

    @property
    def duration_seconds(self) -> float:
        return self.duration_samples / self.sample_rate

    @property
    def pre_trigger_data(self) -> np.ndarray:
        """Pre-trigger segment (channels × trigger_offset). Empty if none."""
        return self.data[:, : self.trigger_offset]

    @property
    def window(self) -> np.ndarray:
        """Post-trigger segment (channels × …) — the main analysis window."""
        return self.data[:, self.trigger_offset :]

    @property
    def has_pre_trigger_data(self) -> bool:
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
            # equal_nan=True keeps reflexivity when data holds NaN
            # (epoch == epoch); safe for integer arrays too.
            and np.array_equal(self.data, other.data, equal_nan=True)
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


@dataclass(frozen=True)
class EpochInfo:
    """Lightweight metadata projection of an Epoch — carries no signal matrix.

    BCISession's trial history stores this instead of the full Epoch, so the
    aggregate grows by only a few dozen bytes per trial rather than retaining a
    whole (channels x samples) array.
    """
    event_id: int
    channel_count: int
    sample_rate: int
    duration_samples: int
    trigger_offset: int

    @classmethod
    def from_epoch(cls, epoch: "Epoch") -> "EpochInfo":
        return cls(
            event_id=epoch.event_id,
            channel_count=epoch.channel_count,
            sample_rate=epoch.sample_rate,
            duration_samples=epoch.duration_samples,
            trigger_offset=epoch.trigger_offset,
        )

    @property
    def duration_seconds(self) -> float:
        return self.duration_samples / self.sample_rate
