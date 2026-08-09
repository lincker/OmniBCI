"""Stimulation IPC lifecycle regression tests (no renderer or sockets)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.application.events import CommandRequest, CommandResult
from omnibci.shared.events import Event, EventType


def test_stop_event_handler_accepts_event_bus_argument() -> None:
    app = Stimulation.__new__(Stimulation)
    app.should_stop = False

    asyncio.run(
        app._stop_stimulation_system(Event(EventType.StopStimulation))
    )

    assert app.should_stop is True


def test_confirmed_stop_flushes_result_before_marking_process_stopped() -> None:
    app = Stimulation.__new__(Stimulation)
    app.should_stop = False
    app.peer_channel = MagicMock()

    async def send(event):
        assert app.should_stop is False
        assert event.type == EventType.CommandResult
        assert event.data == CommandResult(
            command_id="stop-1",
            command="StopStimulation",
            ok=True,
            message="Stimulation stop accepted",
            error_code=None,
        )
        return True

    app.peer_channel.send = AsyncMock(side_effect=send)
    asyncio.run(app._stop_stimulation_system(Event(
        EventType.StopStimulation,
        CommandRequest(command_id="stop-1"),
    )))

    app.peer_channel.send.assert_awaited_once()
    assert app.should_stop is True


def test_render_failure_still_closes_ipc_boundary() -> None:
    class _FailingStimulator:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def start_async(self) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("renderer failed")

        def stop(self) -> None:
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

        def stop(self) -> None:
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

        def stop(self) -> None:
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

        def stop(self) -> None:
            self.stop_calls += 1
            if self.release is not None:
                self.release.set()

    app = Stimulation.__new__(Stimulation)
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
    app._startup_reporter.ready.assert_called_once_with()
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

        def stop(self) -> None:
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

        def stop(self) -> None:
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
