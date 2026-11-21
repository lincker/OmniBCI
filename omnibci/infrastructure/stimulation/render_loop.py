#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""Drive drawing and frame submission on the calling asyncio thread."""
from __future__ import annotations

import asyncio

from omnibci.domain.ports.paradigm import Paradigm
from omnibci.domain.ports.renderer import Renderer


class RenderLoop:
    """Draw fresh display opportunities; Stimulator owns resource cleanup."""

    def __init__(self, renderer: Renderer, paradigm: Paradigm) -> None:
        self._renderer = renderer
        self._paradigm = paradigm
        self._stop_requested = False

    def request_stop(self) -> None:
        """Request termination after the current callback returns."""
        self._stop_requested = True

    async def run_async(self) -> None:
        while not self._stop_requested and not self._renderer.should_close():
            self._renderer.poll_events()
            frame = self._renderer.begin_frame()
            if frame is not None:
                submit = False
                try:
                    if not self._stop_requested:
                        submit = self._paradigm.render_frame(frame)
                        if type(submit) is not bool:
                            submit = False
                            raise TypeError("Paradigm.render_frame must return a bool")
                except BaseException as failure:
                    try:
                        self._renderer.end_frame(frame, submit=False)
                    except BaseException as cleanup:
                        raise BaseExceptionGroup("Drawing and frame discard failed", [failure, cleanup])
                    raise
                self._renderer.end_frame(frame, submit=submit)
            # This sleep only yields to commands/window events. Native display
            # samples, not this timer, determine whether another frame exists.
            await asyncio.sleep(.001)
