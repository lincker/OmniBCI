#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Drive drawing and frame submission on the calling asyncio thread."""
from __future__ import annotations

import asyncio
import time

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Renderer


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
            self._paradigm.render_frame(t)
            self._renderer.end_frame()
            await asyncio.sleep(0)
