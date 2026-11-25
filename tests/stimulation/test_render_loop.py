"""The sole render loop polls, draws and submits on its caller's thread."""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.stimulation.render_loop import RenderLoop


def _make_loop(iterations=0):
    renderer, paradigm = MagicMock(), MagicMock()
    renderer.should_close.side_effect = [False] * iterations + [True]
    return RenderLoop(renderer, paradigm), renderer, paradigm


def test_each_frame_polls_draws_submits_in_order():
    loop, renderer, paradigm = _make_loop(2)
    calls = []
    renderer.poll_events.side_effect = lambda: calls.append('poll')
    paradigm.render_frame.side_effect = lambda elapsed: calls.append('draw')
    renderer.end_frame.side_effect = lambda: calls.append('submit')
    asyncio.run(loop.run_async())
    assert calls == ['poll', 'draw', 'submit'] * 2


def test_elapsed_time_is_nonnegative_monotonic_float():
    loop, _, paradigm = _make_loop(2)
    asyncio.run(loop.run_async())
    times = [call.args[0] for call in paradigm.render_frame.call_args_list]
    assert len(times) == 2
    assert all(isinstance(t, float) and t >= 0 for t in times)
    assert times == sorted(times)


def test_request_stop_ends_async_run_without_resource_cleanup():
    async def scenario():
        loop, renderer, paradigm = _make_loop()
        renderer.should_close.side_effect = None
        renderer.should_close.return_value = False
        task = asyncio.create_task(loop.run_async())
        await asyncio.sleep(0)
        assert not task.done()
        loop.request_stop()
        await task
        paradigm.teardown.assert_not_called()
        renderer.shutdown.assert_not_called()
    asyncio.run(scenario())


def test_stop_requested_before_running_draws_no_frames():
    loop, renderer, paradigm = _make_loop(1)
    loop.request_stop()
    asyncio.run(loop.run_async())
    paradigm.render_frame.assert_not_called()
    renderer.shutdown.assert_not_called()


def test_window_close_leaves_cleanup_to_owner():
    loop, renderer, paradigm = _make_loop()
    asyncio.run(loop.run_async())
    paradigm.render_frame.assert_not_called()
    paradigm.teardown.assert_not_called()
    renderer.shutdown.assert_not_called()


@pytest.mark.parametrize('operation', ['poll_events', 'render_frame', 'end_frame'])
def test_callback_failure_propagates_to_resource_owner(operation):
    loop, renderer, paradigm = _make_loop(1)
    target = paradigm if operation == 'render_frame' else renderer
    getattr(target, operation).side_effect = RuntimeError(operation)
    with pytest.raises(RuntimeError, match=operation):
        asyncio.run(loop.run_async())
    paradigm.teardown.assert_not_called()
    renderer.shutdown.assert_not_called()


def test_callbacks_run_on_calling_thread():
    loop, renderer, paradigm = _make_loop(1)
    threads = []
    renderer.poll_events.side_effect = lambda: threads.append(threading.get_ident())
    paradigm.render_frame.side_effect = lambda elapsed: threads.append(threading.get_ident())
    renderer.end_frame.side_effect = lambda: threads.append(threading.get_ident())
    asyncio.run(loop.run_async())
    assert threads == [threading.get_ident()] * 3
