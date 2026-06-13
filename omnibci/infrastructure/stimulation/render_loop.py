#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""RenderLoop — drives paradigm rendering at VSync rate.

Two execution modes
-------------------
``start()``         — spawns a background thread (non-macOS platforms).
``run_async()``     — coroutine; runs inside the asyncio event loop on the
                      *calling* thread.  Required on macOS where Cocoa's
                      AppKit forbids ``nextEventMatchingMask`` from any thread
                      other than the main thread.  Because ``asyncio.run()``
                      already occupies the main thread, turning the render loop
                      into a coroutine satisfies that constraint with zero
                      extra threads.
"""
from __future__ import annotations

import asyncio
import threading
import time

from omnibci.infrastructure.stimulation.ports.renderer import Renderer


class RenderLoop:
    """Runs a Renderer + Paradigm on a dedicated GL thread.

    The loop calls, in order each iteration:
        1. ``renderer.poll_events()``   — process window / input events
        2. ``paradigm.render_frame(t)`` — paradigm draws its current state
        3. ``renderer.end_frame()``     — swap buffers (blocks until VSync)

    Frame rate is governed by the display's refresh rate via ``swap_interval(1)``
    set inside the renderer; no ``time.sleep`` is needed.

    ``teardown`` and ``shutdown`` are called on the GL thread so all GPU
    resource releases happen in the correct context.

    Usage::

        loop = RenderLoop(renderer, paradigm)
        loop.start()
        # ... experiment runs ...
        loop.stop()
    """

    def __init__(self, renderer: Renderer, paradigm) -> None:
        self._renderer    = renderer
        self._paradigm    = paradigm
        self._stop_event  = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the GL thread and begin rendering."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="RenderLoop", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the loop to stop and wait for the GL thread to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── asyncio coroutine (macOS / single-thread mode) ─────────────────────

    async def run_async(self) -> None:
        """Run the render loop as an asyncio coroutine on the calling thread.

        ``await asyncio.sleep(0)`` yields after each frame so other coroutines
        (IPC, command dispatch) can run.  Frame pacing is still governed by
        VSync (``swap_interval(1)`` inside the renderer).
        """
        t0 = time.perf_counter()
        try:
            while not self._stop_event.is_set() and not self._renderer.should_close():
                t = time.perf_counter() - t0
                self._renderer.poll_events()
                self._paradigm.render_frame(t)
                self._renderer.end_frame()
                await asyncio.sleep(0)
        finally:
            self._paradigm.teardown(self._renderer)
            self._renderer.shutdown()

    # ── GL thread ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        t0 = time.perf_counter()
        try:
            while not self._stop_event.is_set():
                if self._renderer.should_close():
                    break
                t = time.perf_counter() - t0
                self._renderer.poll_events()
                self._paradigm.render_frame(t)
                self._renderer.end_frame()
        finally:
            # Always clean up on the GL thread
            self._paradigm.teardown(self._renderer)
            self._renderer.shutdown()
