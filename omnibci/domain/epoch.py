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
        # 冻结为不可变值对象：拷贝一份**自有**数组并置只读（P2-10）。
        # 原实现直接对传入数组设 writeable=False——就地改调用者的 buffer，
        # 上游若还想写它会莫名 ValueError（潜伏副作用）；且 object.__setattr__
        # 把 data 设成它自身是无效语句。改为拷贝：调用者数组不受影响，
        # Epoch 持有的副本只读。（唯一生产调用方 EpochProcessor 因此无需
        # 再自行 .copy()。）
        frozen = np.array(self.data, copy=True)
        frozen.flags.writeable = False
        object.__setattr__(self, "data", frozen)

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


@dataclass(frozen=True)
class EpochInfo:
    """Epoch 的轻量元数据投影 —— 不携带信号矩阵（P1-6）。

    BCISession 的 trial 历史保存它而不是完整 Epoch：长会话中聚合根因此
    只随 trial 数线性增长几十字节，而不是每 trial 保留一整块
    (channels × samples) 的信号数据。需要信号本体的消费者应订阅
    TrialCompleted 领域事件（瞬态携带完整 Epoch），在事件传递时处理。
    """
    event_id: int
    channel_count: int
    sample_rate: int
    duration_samples: int
    trigger_offset: int
    created_at: datetime

    @classmethod
    def from_epoch(cls, epoch: "Epoch") -> "EpochInfo":
        return cls(
            event_id=epoch.event_id,
            channel_count=epoch.channel_count,
            sample_rate=epoch.sample_rate,
            duration_samples=epoch.duration_samples,
            trigger_offset=epoch.trigger_offset,
            created_at=epoch.created_at,
        )

    @property
    def duration_seconds(self) -> float:
        return self.duration_samples / self.sample_rate
