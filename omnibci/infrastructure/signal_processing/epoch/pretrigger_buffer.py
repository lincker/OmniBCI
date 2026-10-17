"""
PreTriggerBuffer — rolling store of the most-recent pre-trigger samples.

Why this exists:
  Pre-trigger data lives *before* the trigger. The detection loop used to discard
  every chunk seen while idle, so by the time a start marker fired the
  pre-trigger signal was already gone.  This buffer keeps the last ``capacity``
  samples from the continuous stream, including active epochs, so that a
  trigger always receives the same reference data regardless of chunk boundaries.

  ``capacity`` is derived from the pre-trigger capture duration (legacy key
  ``baseline_duration``) and the current signal sample rate:
  ``pre_trigger_samples = round(baseline_duration * sample_rate)``. A capacity
  of 0 disables pre-trigger capture entirely (the legacy behaviour).
"""

from typing import Optional

import numpy as np


class PreTriggerBuffer:
    """Channels × ≤capacity ring of the most recent pre-trigger samples."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(0, int(capacity))
        self._buf: Optional[np.ndarray] = None  # shape: channels × ≤capacity

    def push(self, data: np.ndarray) -> None:
        """Append samples (channels × n) and keep only the trailing ``capacity``."""
        if self.capacity == 0 or data.shape[1] == 0:
            return
        if self._buf is None:
            self._buf = data[:, -self.capacity:].copy()
        else:
            self._buf = np.concatenate([self._buf, data], axis=1)[:, -self.capacity:].copy()

    def take_pre_trigger_data(self, extra: np.ndarray) -> np.ndarray:
        """
        Return up to ``capacity`` samples ending exactly at the trigger.

        ``extra`` is the pre-trigger slice from the trigger-bearing chunk
        (``chunk.data[:, :start_idx]``); it also fixes the channel count for the
        empty/disabled case.  The result is a fresh array safe to freeze.
        """
        if self.capacity == 0:
            return extra[:, :0].copy()
        parts = [p for p in (self._buf, extra) if p is not None and p.shape[1] > 0]
        if not parts:
            return extra[:, :0].copy()
        combined = parts[0] if len(parts) == 1 else np.concatenate(parts, axis=1)
        return combined[:, -self.capacity:].copy()

    def clear(self) -> None:
        self._buf = None
