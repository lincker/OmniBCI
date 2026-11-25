"""Stimulation IPC lifecycle regression tests (no renderer or sockets)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.execution_helpers import synthetic_execution_snapshot
from omnibci.infrastructure.plugin_management.preparation import execution_receipt

from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.application.events import CommandRequest
from omnibci.shared.events import Event, EventType


def test_stop_event_handler_accepts_event_bus_argument() -> None:
    app = Stimulation.__new__(Stimulation)
    app.should_stop = False

    asyncio.run(
        app._stop_stimulation_system(Event(EventType.StopStimulation))
    )

    assert app.should_stop is True


def test_stop_records_request_without_premature_success() -> None:
    app = Stimulation.__new__(Stimulation)
    app.should_stop = False
    app._stop_requests = []
    app.peer_channel = MagicMock()
    app.peer_channel.send = AsyncMock()
    request = CommandRequest(command_id="stop-1")
    asyncio.run(app._stop_stimulation_system(Event(EventType.StopStimulation, request)))
    assert app.should_stop is True
    assert app._paused is True
    assert app._stop_requests == [request]
    app.peer_channel.send.assert_not_awaited()


def test_render_failure_still_closes_ipc_boundary() -> None:
    class _FailingStimulator:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def start_async(self) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("renderer failed")

        def request_stop(self) -> None:
            self.stop_calls += 1

    app = Stimulation.__new__(Stimulation)
    app.should_stop = False
    app.stimulator = _FailingStimulator()
    app._render_task = None
    app._startup_reporter = MagicMock()
    app.peer_channel = MagicMock()

    async def wait_for_engine(*_args) -> None:
        await asyncio.Future()

    app.peer_channel.wait_until_connected = AsyncMock(side_effect=wait_for_engine)
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.execute_command = AsyncMock()

    with pytest.raises(RuntimeError, match="renderer failed"):
        asyncio.run(app.serve())

    assert app.stimulator.stop_calls == 1
    app._startup_reporter.ready.assert_not_called()
    failed = app._startup_reporter.failed.call_args
    assert failed.args[0] == "initialization"
    assert isinstance(failed.args[1], RuntimeError)
    app.stop.assert_awaited_once()


def test_pre_set_stop_event_does_not_start_renderer_or_ipc() -> None:
    class _StopEvent:
        def is_set(self) -> bool:
            return True

    class _Stimulator:
        def __init__(self) -> None:
            self.start_calls = 0
            self.stop_calls = 0

        async def start_async(self) -> None:
            self.start_calls += 1
            await asyncio.Future()  # pragma: no cover - must never start

        def request_stop(self) -> None:
            self.stop_calls += 1

    app = Stimulation.__new__(Stimulation)
    app.should_stop = False
    app._stop_event = _StopEvent()
    app.stimulator = _Stimulator()
    app._render_task = None
    app.start = AsyncMock()
    app.stop = AsyncMock()

    asyncio.run(asyncio.wait_for(app.serve(), timeout=0.2))

    app.start.assert_not_awaited()
    assert app.stimulator.start_calls == 0
    app.stop.assert_awaited_once()


def test_stop_race_waits_until_renderer_has_installed_stop_target() -> None:
    class _StopAfterRenderScheduled:
        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls >= 3

    class _Stimulator:
        def __init__(self) -> None:
            self.started = False
            self.stop_calls = 0
            self.release = None

        async def start_async(self) -> None:
            self.release = asyncio.Event()
            self.started = True
            await self.release.wait()

        def request_stop(self) -> None:
            self.stop_calls += 1
            if self.release is not None:
                self.release.set()

    app = Stimulation.__new__(Stimulation)
    app.should_stop = False
    app._stop_event = _StopAfterRenderScheduled()
    app.stimulator = _Stimulator()
    app._render_task = None
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.execute_command = AsyncMock()

    asyncio.run(asyncio.wait_for(app.serve(), timeout=0.2))

    assert app.stimulator.started is True
    assert app.stimulator.stop_calls == 1
    app.stop.assert_awaited_once()


def test_managed_start_reports_ready_after_renderer_and_link() -> None:
    class _StopAfterReady:
        def __init__(self) -> None:
            self.calls = 0

        def is_set(self) -> bool:
            self.calls += 1
            return self.calls >= 3

    class _Stimulator:
        def __init__(self) -> None:
            self.release = None
            self.stop_calls = 0

        async def start_async(self) -> None:
            self.release = asyncio.Event()
            await self.release.wait()

        def request_stop(self) -> None:
            self.stop_calls += 1
            if self.release is not None:
                self.release.set()

    app = Stimulation.__new__(Stimulation)
    app._runtime_snapshot = synthetic_execution_snapshot({
        "CONFIG.module_revisions": {"stimulation": "a" * 64},
        "CONFIG.engine_revision": "b" * 64, "CONFIG.generation": "run-1"})
    app.should_stop = False
    app._stop_event = _StopAfterReady()
    app._startup_reporter = MagicMock()
    app.stimulator = _Stimulator()
    app.peer_channel = MagicMock()
    app.peer_channel.wait_until_connected = AsyncMock()
    app._render_task = None
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.execute_command = AsyncMock()

    asyncio.run(asyncio.wait_for(app.serve(), timeout=0.2))

    app.peer_channel.wait_until_connected.assert_awaited_once()
    app._startup_reporter.ready.assert_called_once_with(execution=execution_receipt(app._runtime_snapshot, "stimulation", "prepare-1"))
    app._startup_reporter.failed.assert_not_called()
    assert app.stimulator.stop_calls == 1
    app.stop.assert_awaited_once()


def test_managed_link_timeout_reports_failed_and_closes_renderer() -> None:
    class _Stimulator:
        def __init__(self) -> None:
            self.release = None
            self.stop_calls = 0

        async def start_async(self) -> None:
            self.release = asyncio.Event()
            await self.release.wait()

        def request_stop(self) -> None:
            self.stop_calls += 1
            if self.release is not None:
                self.release.set()

    app = Stimulation.__new__(Stimulation)
    app.should_stop = False
    app._stop_event = None
    app._startup_reporter = MagicMock()
    app.stimulator = _Stimulator()
    app.peer_channel = MagicMock()
    app.peer_channel.wait_until_connected = AsyncMock(
        side_effect=TimeoutError("engine link unavailable")
    )
    app._render_task = None
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.execute_command = AsyncMock()

    with pytest.raises(TimeoutError, match="link unavailable"):
        asyncio.run(app.serve())

    app._startup_reporter.ready.assert_not_called()
    failed = app._startup_reporter.failed.call_args
    assert failed.args[0] == "initialization"
    assert isinstance(failed.args[1], TimeoutError)
    assert app.stimulator.stop_calls == 1
    app.stop.assert_awaited_once()


def test_owned_event_queue_is_closed_once() -> None:
    class _Queue:
        def __init__(self) -> None:
            self.close_calls = 0
            self.cancel_join_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        def cancel_join_thread(self) -> None:
            self.cancel_join_calls += 1

    app = Stimulation.__new__(Stimulation)
    app._event_queue = _Queue()
    app._event_queue_closed = False

    app._close_event_queue()
    app._close_event_queue()

    assert app._event_queue.close_calls == 1
    assert app._event_queue.cancel_join_calls == 1


def test_render_stop_failure_still_closes_ipc_boundary() -> None:
    class _BrokenStimulator:
        async def start_async(self) -> None:
            await asyncio.Future()

        def request_stop(self) -> None:
            raise RuntimeError("renderer stop failed")

    app = Stimulation.__new__(Stimulation)
    app.should_stop = True
    app.stimulator = _BrokenStimulator()
    app._render_task = None
    app.start = AsyncMock()
    app.stop = AsyncMock()

    with pytest.raises(RuntimeError, match="renderer stop failed"):
        asyncio.run(app.serve())

    app.stop.assert_awaited_once()


def test_event_queue_close_failure_keeps_cleanup_obligation() -> None:
    class _Queue:
        def cancel_join_thread(self) -> None:
            pass

        def close(self) -> None:
            raise OSError("close failed")

    app = Stimulation.__new__(Stimulation)
    app._event_queue = _Queue()
    app._event_queue_closed = False

    with pytest.raises(ExceptionGroup, match="queue cleanup failed") as raised:
        app._close_event_queue()

    assert isinstance(raised.value.exceptions[0], OSError)
    assert app._event_queue_closed is False


from queue import Queue
from unittest.mock import Mock


def _pause_app():
    instance = Stimulation.__new__(Stimulation)
    instance.should_stop = False
    instance.stim_command_queue = Queue()
    instance.result_command_queue = Queue()
    instance.event_bus = Mock()
    instance.stimulator = Mock()
    instance._runtime_snapshot = {"CONFIG.module_revisions": {"stimulation": "revision"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "generation"}
    instance._paused = False
    return instance


def _pause_receipts(instance, command):
    return [call.args[0].data for call in instance.event_bus.put_event.call_args_list
            if call.args[0].type == EventType.CommandResult
            and call.args[0].data.command == command.name]


def test_pause_joins_renderer_clears_trigger_and_cancels_future_trials():
    async def scenario():
        instance = _pause_app()
        exited = asyncio.Event()
        instance._render_task = asyncio.create_task(exited.wait())
        instance._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "queued-trials", {"n_trials": 2, "trial_params": [{"event_id": 1}, {"event_id": 2}]},
        )))
        pause = asyncio.create_task(instance._pause_stimulation(Event(
            EventType.PauseStimulation, CommandRequest("pause"),
        )))
        await asyncio.sleep(0)
        assert not pause.done()
        assert _pause_receipts(instance, EventType.PauseStimulation) == []
        instance.stimulator.request_stop.assert_called_once()
        instance.stimulator.clear_trigger.assert_not_called()
        assert instance.stim_command_queue.empty()
        assert _pause_receipts(instance, EventType.StartStimTrial)[0].ok is False
        await instance.execute_command()
        instance.stimulator.present_trial.assert_not_called()
        exited.set()
        await pause
        instance.stimulator.clear_trigger.assert_not_called()
        result = _pause_receipts(instance, EventType.PauseStimulation)[0]
        assert result.command_id == "pause"
        assert result.ok
        assert result.data == {
            "paused": True, "renderer_stopped": True, "cancelled_trials": 2,
            "generation": "generation", "module_revision": "revision", "engine_revision": "engine",
        }
        instance._put_stim_command_queue(Event(EventType.StartStimTrial, CommandRequest(
            "late-trial", {"n_trials": 1, "trial_params": [{"event_id": 0}]},
        )))
        assert instance.stim_command_queue.empty()
        assert _pause_receipts(instance, EventType.StartStimTrial)[-1].ok is False

    asyncio.run(scenario())


def test_trigger_clear_failure_leaves_paused_without_success_receipt():
    async def scenario():
        instance = _pause_app()
        async def failed_cleanup():
            raise OSError("trigger clear failed")
        instance._render_task = asyncio.create_task(failed_cleanup())
        await instance._pause_stimulation(Event(EventType.PauseStimulation, CommandRequest("pause")))
        result = _pause_receipts(instance, EventType.PauseStimulation)[0]
        assert not result.ok
        assert "trigger clear failed" in result.message
        assert result.data is None
        assert instance._paused

    asyncio.run(scenario())


def test_renderer_failure_is_not_a_successful_pause():
    async def scenario():
        instance = _pause_app()

        async def failed_renderer():
            raise RuntimeError("renderer teardown failed")

        instance._render_task = asyncio.create_task(failed_renderer())
        await instance._pause_stimulation(Event(EventType.PauseStimulation, CommandRequest("pause")))
        result = _pause_receipts(instance, EventType.PauseStimulation)[0]
        assert not result.ok
        assert "renderer teardown failed" in result.message
        assert instance._paused
        instance.stimulator.clear_trigger.assert_not_called()

    asyncio.run(scenario())


@pytest.mark.parametrize('cleanup_error', [None, RuntimeError('trigger cleanup failed')])
def test_stop_receipt_follows_cleanup_and_precedes_ipc_close(cleanup_error):
    async def scenario():
        app = Stimulation(settings_snapshot={})
        calls = []
        release = asyncio.Event()
        requested = asyncio.Event()

        async def render():
            await release.wait()
            calls.append('cleanup')
            if cleanup_error:
                raise cleanup_error

        def request_stop():
            requested.set()

        async def send(event):
            if event.type == EventType.CommandResult:
                assert calls == ['cleanup']
                assert event.data.command_id == 'stop'
                assert event.data.ok is (cleanup_error is None)
                calls.append('receipt')
            return True

        app.start = AsyncMock()
        app._await_managed_readiness = AsyncMock()
        app.stimulator = Mock()
        app.stimulator.start_async = render
        app.stimulator.request_stop = request_stop
        app.peer_channel.send = AsyncMock(side_effect=send)
        app.stop = AsyncMock(side_effect=lambda: calls.append('ipc-close'))
        async def command():
            await app._stop_stimulation_system(Event(EventType.StopStimulation, CommandRequest('stop')))
        app.execute_command = command
        task = asyncio.create_task(app.serve())
        try:
            await asyncio.wait_for(requested.wait(), .5)
            assert calls == []
            assert not task.done()
            release.set()
            if cleanup_error:
                with pytest.raises(RuntimeError, match='trigger cleanup failed'):
                    await task
            else:
                await task
            assert calls == ['cleanup', 'receipt', 'ipc-close']
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            app._close_event_queue()
    asyncio.run(scenario())


def test_repeated_cancellation_cannot_close_ipc_before_render_settles():
    async def scenario():
        app = Stimulation(settings_snapshot={})
        release, started = asyncio.Event(), asyncio.Event()
        calls = []

        async def render():
            started.set()
            await release.wait()
            calls.append('cleanup')

        app.start = AsyncMock()
        app._await_managed_readiness = AsyncMock()
        app.execute_command = AsyncMock()
        app.stimulator = Mock()
        app.stimulator.start_async = render
        app.stop = AsyncMock(side_effect=lambda: calls.append('ipc-close'))
        task = asyncio.create_task(app.serve())
        try:
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert calls == []
            assert not app._render_task.cancelled()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert calls == ['cleanup', 'ipc-close']
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            app._close_event_queue()
    asyncio.run(scenario())
