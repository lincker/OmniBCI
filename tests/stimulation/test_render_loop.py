"""
Unit tests for RenderLoop.

The renderer and paradigm are MagicMocks.  We control loop iteration count
via should_close() returning True after N calls, then join with a short
timeout so tests stay fast.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call

import pytest

from omnibci.infrastructure.stimulation.render_loop import RenderLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_loop(iterations: int = 0):
    """Return (loop, renderer, paradigm) that exits after *iterations* frames."""
    renderer = MagicMock()
    paradigm = MagicMock()

    count = {"n": 0}
    def should_close():
        count["n"] += 1
        return count["n"] > iterations

    renderer.should_close.side_effect = should_close
    loop = RenderLoop(renderer, paradigm)
    return loop, renderer, paradigm


# ---------------------------------------------------------------------------
# Thread lifecycle
# ---------------------------------------------------------------------------

class TestRenderLoopLifecycle:
    def test_start_spawns_thread(self):
        loop, _, _ = _make_loop(0)
        loop.start()
        loop.stop(timeout=1.0)
        # No assertion needed — if it hangs, the test itself times out.

    def test_is_running_true_after_start(self):
        renderer = MagicMock()
        paradigm = MagicMock()
        ready    = threading.Event()
        gate     = threading.Event()

        renderer.should_close.return_value = False
        renderer.end_frame.side_effect = lambda: ready.set() or gate.wait()

        loop = RenderLoop(renderer, paradigm)
        loop.start()
        ready.wait(timeout=1.0)
        assert loop.is_running
        gate.set()
        loop.stop(timeout=1.0)

    def test_is_running_false_after_stop(self):
        loop, _, _ = _make_loop(0)
        loop.start()
        loop.stop(timeout=1.0)
        assert not loop.is_running

    def test_stop_without_start_is_noop(self):
        loop, _, _ = _make_loop()
        loop.stop()   # should not raise


# ---------------------------------------------------------------------------
# Per-frame call order
# ---------------------------------------------------------------------------

class TestRenderLoopCallOrder:
    def test_poll_render_end_called_each_frame(self):
        loop, renderer, paradigm = _make_loop(iterations=2)
        loop.start()
        loop.stop(timeout=1.0)

        assert renderer.poll_events.call_count  >= 1
        assert paradigm.render_frame.call_count >= 1
        assert renderer.end_frame.call_count    >= 1

    def test_render_frame_receives_float_time(self):
        loop, renderer, paradigm = _make_loop(iterations=1)
        loop.start()
        loop.stop(timeout=1.0)

        args = paradigm.render_frame.call_args[0]
        assert len(args) == 1
        assert isinstance(args[0], float)

    def test_time_argument_is_non_negative(self):
        loop, renderer, paradigm = _make_loop(iterations=1)
        loop.start()
        loop.stop(timeout=1.0)

        t = paradigm.render_frame.call_args[0][0]
        assert t >= 0.0


# ---------------------------------------------------------------------------
# Cleanup on exit
# ---------------------------------------------------------------------------

class TestRenderLoopCleanup:
    def test_teardown_called_on_should_close(self):
        loop, renderer, paradigm = _make_loop(iterations=0)
        loop.start()
        loop.stop(timeout=1.0)
        paradigm.teardown.assert_called_once_with(renderer)

    def test_shutdown_called_on_should_close(self):
        loop, renderer, paradigm = _make_loop(iterations=0)
        loop.start()
        loop.stop(timeout=1.0)
        renderer.shutdown.assert_called_once()

    def test_teardown_called_on_stop_signal(self):
        renderer = MagicMock()
        paradigm = MagicMock()
        renderer.should_close.return_value = False

        loop = RenderLoop(renderer, paradigm)
        loop.start()
        time.sleep(0.05)
        loop.stop(timeout=1.0)

        paradigm.teardown.assert_called_once_with(renderer)
        renderer.shutdown.assert_called_once()
