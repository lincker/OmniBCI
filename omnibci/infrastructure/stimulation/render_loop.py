#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Drive drawing and frame submission on the calling asyncio thread."""
from __future__ import annotations

import asyncio
import logging
import math
import time

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Renderer

logger = logging.getLogger(__name__)
_RATE_LOG_INTERVAL_SECONDS = 30.0


def required_refresh_rate_hz(paradigm) -> float | None:
    """Return the nominal display rate (Hz) required by *paradigm*, if any."""
    if "required_refresh_rate_hz" in vars(paradigm):
        value = vars(paradigm)["required_refresh_rate_hz"]
    else:
        value = getattr(type(paradigm), "required_refresh_rate_hz", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Paradigm required_refresh_rate_hz must be a positive finite number")
    return float(value)


def validate_display_refresh_rate(renderer, paradigm) -> float | None:
    """Verify at startup that the system nominal display rate matches *paradigm*.

    Uses the renderer's system-reported nominal (integer-Hz) monitor refresh
    rate rather than sub-Hz native display queries. Logs the reported rate and
    returns it, or raises ``RuntimeError`` on mismatch so startup aborts.
    """
    required_hz = required_refresh_rate_hz(paradigm)
    query = getattr(renderer, "refresh_rate_hz", None)
    observed_hz = query() if callable(query) else None
    valid_observed = (
        observed_hz is not None
        and not isinstance(observed_hz, bool)
        and isinstance(observed_hz, (int, float))
        and math.isfinite(observed_hz)
        and observed_hz > 0
    )
    if required_hz is None:
        if valid_observed:
            logger.info(
                "Display refresh rate reported by system: %g Hz (%s has no fixed rate requirement)",
                observed_hz,
                type(paradigm).__name__,
            )
            return float(observed_hz)
        return None
    if not valid_observed or round(observed_hz) != round(required_hz):
        if observed_hz is None:
            observed = "unavailable"
        elif isinstance(observed_hz, (int, float)) and not isinstance(observed_hz, bool):
            observed = f"{observed_hz:g} Hz"
        else:
            observed = repr(observed_hz)
        raise RuntimeError(
            f"{type(paradigm).__name__} requires a nominal {required_hz:g} Hz display mode "
            f"(system reported {observed})"
        )
    logger.info(
        "Display refresh rate verified: system=%g Hz, paradigm_required=%g Hz (%s)",
        observed_hz,
        required_hz,
        type(paradigm).__name__,
    )
    return float(observed_hz)


class RenderLoop:
    """Poll, draw, submit, then yield; Stimulator owns resource cleanup."""

    def __init__(self, renderer: Renderer, paradigm: Paradigm) -> None:
        self._renderer = renderer
        self._paradigm = paradigm
        self._stop_requested = False

    def request_stop(self) -> None:
        """Request termination after the current callback returns."""
        self._stop_requested = True

    def _log_measured_refresh_rate(
        self,
        *,
        label: str,
        frames: int,
        elapsed: float,
        required_hz: float | None,
    ) -> None:
        if frames < 2 or elapsed <= 0:
            return
        measured_hz = (frames - 1) / elapsed
        if required_hz is not None:
            logger.info(
                "RenderLoop %s refresh rate: %.2f Hz over %d frames (%.2fs, paradigm_required=%g Hz)",
                label,
                measured_hz,
                frames,
                elapsed,
                required_hz,
            )
        else:
            logger.info(
                "RenderLoop %s refresh rate: %.2f Hz over %d frames (%.2fs)",
                label,
                measured_hz,
                frames,
                elapsed,
            )

    async def run_async(self) -> None:
        try:
            required_hz = required_refresh_rate_hz(self._paradigm)
        except ValueError:
            required_hz = None
        t0 = time.perf_counter()
        first_t: float | None = None
        last_t: float | None = None
        frame_count = 0
        window_start: float | None = None
        window_frames = 0
        try:
            while not self._stop_requested and not self._renderer.should_close():
                t = time.perf_counter() - t0
                if first_t is None:
                    first_t = window_start = t
                last_t = t
                frame_count += 1
                window_frames += 1
                if window_start is not None and t - window_start >= _RATE_LOG_INTERVAL_SECONDS:
                    self._log_measured_refresh_rate(
                        label="measured",
                        frames=window_frames,
                        elapsed=t - window_start,
                        required_hz=required_hz,
                    )
                    window_start = t
                    window_frames = 1
                self._renderer.poll_events()
                self._paradigm.render_frame(t)
                self._renderer.end_frame()
                await asyncio.sleep(0)
        finally:
            if first_t is not None and last_t is not None:
                self._log_measured_refresh_rate(
                    label="session measured",
                    frames=frame_count,
                    elapsed=last_t - first_t,
                    required_hz=required_hz,
                )
