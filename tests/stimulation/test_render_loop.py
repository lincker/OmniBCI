"""The sole render loop polls, draws and submits on its caller's thread."""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.stimulation.render_loop import RenderLoop
from omnibci.sdk import FrameTiming


def test_pending_display_notification_never_draws_an_extra_frame():
    loop, renderer, paradigm = _make_loop(3)
    frames = [FrameTiming(refresh_index=i, target_time=i / 60, refresh_period=1 / 60)
              for i in (100, 104)]
    renderer.begin_frame.side_effect = [frames[0], None, frames[1]]
    paradigm.render_frame.return_value = True
    asyncio.run(loop.run_async())
    assert [c.args[0] for c in paradigm.render_frame.call_args_list] == frames
    assert renderer.end_frame.call_count == 2


def test_unavailable_image_is_discarded_and_next_target_is_used():
    loop, renderer, paradigm = _make_loop(2)
    frames = [FrameTiming(refresh_index=i, target_time=i / 60, refresh_period=1 / 60)
              for i in (100, 101)]
    renderer.begin_frame.side_effect = frames
    paradigm.render_frame.side_effect = [False, True]
    asyncio.run(loop.run_async())
    assert [(c.args[0], c.kwargs['submit']) for c in renderer.end_frame.call_args_list] == [
        (frames[0], False), (frames[1], True)]


@pytest.mark.parametrize('result', [None, 1, 'ready'])
def test_old_or_invalid_callback_result_is_rejected_and_discarded(result):
    loop, renderer, paradigm = _make_loop(1)
    paradigm.render_frame.return_value = result
    with pytest.raises(TypeError, match='bool'):
        asyncio.run(loop.run_async())
    renderer.end_frame.assert_called_once_with(renderer.begin_frame.return_value, submit=False)


def test_stop_while_waiting_for_display_does_not_create_a_frame():
    async def scenario():
        loop, renderer, paradigm = _make_loop()
        renderer.should_close.side_effect = None
        renderer.should_close.return_value = False
        renderer.begin_frame.return_value = None
        task = asyncio.create_task(loop.run_async())
        await asyncio.sleep(.005)
        loop.request_stop()
        await asyncio.wait_for(task, .1)
        paradigm.render_frame.assert_not_called()
        renderer.end_frame.assert_not_called()
    asyncio.run(scenario())


def _make_loop(iterations=0):
    renderer, paradigm = MagicMock(), MagicMock()
    renderer.should_close.side_effect = [False] * iterations + [True]
    renderer.begin_frame.return_value = FrameTiming(1, 1 / 60, 1 / 60)
    paradigm.render_frame.return_value = True
    return RenderLoop(renderer, paradigm), renderer, paradigm


def test_each_frame_polls_draws_submits_in_order():
    loop, renderer, paradigm = _make_loop(2)
    calls = []
    renderer.poll_events.side_effect = lambda: calls.append('poll')
    paradigm.render_frame.side_effect = lambda frame: calls.append('draw') or True
    renderer.end_frame.side_effect = lambda frame, *, submit: calls.append('submit')
    asyncio.run(loop.run_async())
    assert calls == ['poll', 'draw', 'submit'] * 2


def test_native_timing_is_passed_without_rebasing_or_filling_gaps():
    loop, renderer, paradigm = _make_loop(2)
    frames = [FrameTiming(10, 1, 1 / 60), FrameTiming(13, 1.05, 1 / 60)]
    renderer.begin_frame.side_effect = frames
    asyncio.run(loop.run_async())
    times = [call.args[0] for call in paradigm.render_frame.call_args_list]
    assert times == frames


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
    paradigm.render_frame.side_effect = lambda frame: threads.append(threading.get_ident()) or True
    renderer.end_frame.side_effect = lambda frame, *, submit: threads.append(threading.get_ident())
    asyncio.run(loop.run_async())
    assert threads == [threading.get_ident()] * 3
