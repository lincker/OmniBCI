"""Host-only display opportunity accounting shared by the GL adapters."""
from __future__ import annotations

import logging
import math

from omnibci.domain.ports.renderer import FrameTiming


class FrameClock:
    """Consume the latest native sample once; never manufacture refreshes."""

    def __init__(self, source) -> None:
        self._source = source
        self._observed: FrameTiming | None = None
        self._consumed = -1
        self._pending: FrameTiming | None = None
        self._last_change = source.now()
        self._closed = False
        self.skipped_refreshes = 0
        self.discarded_frames = 0
        self.submission_attempts = 0

    def _read(self) -> FrameTiming | None:
        if self._closed:
            raise RuntimeError("Display clock is closed")
        frame = self._source.read()
        now = self._source.now()
        previous = self._observed
        if frame is not None:
            if previous is not None:
                if (frame.refresh_index < previous.refresh_index
                        or not math.isclose(frame.refresh_period, previous.refresh_period, rel_tol=1e-4)
                        or (frame.refresh_index > previous.refresh_index
                            and frame.target_time <= previous.target_time)):
                    raise RuntimeError("Display clock or refresh mode changed")
            if previous is None or frame.refresh_index > previous.refresh_index:
                self._last_change = now
            self._observed = frame
        if now - self._last_change >= 1.0:
            raise RuntimeError("Display clock stopped advancing")
        return frame

    def begin_frame(self) -> FrameTiming | None:
        if self._pending is not None:
            raise RuntimeError("A display frame is already pending")
        frame = self._read()
        if frame is None or frame.refresh_index == self._consumed:
            return None
        if self._consumed >= 0:
            self.skipped_refreshes += frame.refresh_index - self._consumed - 1
        self._consumed = frame.refresh_index
        if self._source.now() >= frame.target_time:
            self.discarded_frames += 1
            return None
        self._pending = frame
        return frame

    def end_frame(self, frame: FrameTiming, *, submit: bool) -> bool:
        if self._pending is not frame:
            raise RuntimeError("Frame does not match the pending display opportunity")
        self._pending = None
        if type(submit) is not bool:
            raise TypeError("submit must be a bool")
        current = self._read()
        if (not submit or current is None or current.refresh_index != frame.refresh_index
                or self._source.now() >= frame.target_time):
            self.discarded_frames += 1
            return False
        self.submission_attempts += 1
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._source.close()
        self._closed = True
        self._pending = None
        logging.info("Display opportunities: skipped=%d discarded=%d submission_attempts=%d; "
                     "OpenGL submissions are not presentation receipts",
                     self.skipped_refreshes, self.discarded_frames, self.submission_attempts)


def create_frame_clock(window) -> FrameClock:
    # Native imports stay out of the domain and the public SDK.
    from omnibci.infrastructure.stimulation.native_display_clock import create_source
    return FrameClock(create_source(window))
