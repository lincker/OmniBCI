"""Stop receipts confirm renderer settlement before closing the peer channel."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.shared.events import Event, EventType


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
