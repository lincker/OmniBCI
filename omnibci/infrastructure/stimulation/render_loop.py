#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Drive drawing and frame submission on the calling asyncio thread."""
from __future__ import annotations

import asyncio
import math
import time

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Renderer


def required_refresh_rate_hz(paradigm) -> float | None:
    """Return the fixed display rate (Hz) required by *paradigm*, if any."""
    if "required_refresh_rate_hz" in vars(paradigm):
        value = vars(paradigm)["required_refresh_rate_hz"]
    else:
        value = getattr(type(paradigm), "required_refresh_rate_hz", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Paradigm required_refresh_rate_hz must be a positive finite number")
    return float(value)


def validate_display_refresh_rate(renderer, paradigm) -> None:
    """Ensure the active window display mode matches *paradigm*'s fixed rate."""
    required_hz = required_refresh_rate_hz(paradigm)
    if required_hz is None:
        return
    query = getattr(renderer, "refresh_rate_hz", None)
    observed_hz = query() if callable(query) else None
    if (
        observed_hz is None
        or isinstance(observed_hz, bool)
        or not isinstance(observed_hz, (int, float))
        or not math.isfinite(observed_hz)
        or not math.isclose(observed_hz, required_hz, rel_tol=0, abs_tol=0.01)
    ):
        observed = "unavailable" if observed_hz is None else f"{observed_hz:g} Hz"
        raise RuntimeError(
            f"{type(paradigm).__name__} requires a fixed {required_hz:g} Hz display mode "
            f"(observed {observed})"
        )


class RenderLoop:
    """Poll, draw, submit, then yield; Stimulator owns resource cleanup."""

    def __init__(self, renderer: Renderer, paradigm: Paradigm) -> None:
        self._renderer = renderer
        self._paradigm = paradigm
        self._stop_requested = False

    def request_stop(self) -> None:
        """Request termination after the current callback returns."""
        self._stop_requested = True

    async def run_async(self) -> None:
        t0 = time.perf_counter()
        while not self._stop_requested and not self._renderer.should_close():
            t = time.perf_counter() - t0
            self._renderer.poll_events()
            validate_display_refresh_rate(self._renderer, self._paradigm)
            self._paradigm.render_frame(t)
            self._renderer.end_frame()
            await asyncio.sleep(0)
