"""Transport loss invalidates the source identity without erasing continuity loss."""
import asyncio
import queue

import pytest

from omnibci.application.events import CommandFailureKind, CommandRequest
from omnibci.infrastructure.acquisition.acquisition import (
    AcquisitionCommand,
    AcquisitionRuntime,
)
from omnibci.shared.events import EventType


class Source:
    def __init__(self):
        self.is_connected = True
        self.is_streaming = False
        self.calls = []

    async def connect(self):
        self.calls.append("connect")
        self.is_connected = True

    async def disconnect(self):
        self.calls.append("disconnect")
        self.is_connected = False
        self.is_streaming = False

    async def start_stream(self):
        self.calls.append("start_stream")
        self.is_streaming = True

    async def stop_stream(self):
        self.calls.append("stop_stream")
        self.is_streaming = False


class Bus:
    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


class Sink:
    def __init__(self):
        self.items = []

    def put_data(self, item):
        self.items.append(item)


def runtime(source=None, *, projected=True):
    acquisition = AcquisitionRuntime(
        source or Source(), queue.Queue(), Sink(), Bus() if projected else None,
        source_id="test-source", preparation_required=False,
        generation="generation", config_revision="revision",
    )
    acquisition._raw_metadata = "metadata from the previous connection"
    acquisition._descriptor = object()
    acquisition._descriptor_error = "previous description warning"
    return acquisition


def assert_disconnected(acquisition):
    assert acquisition.connection_id is None
    assert acquisition._raw_metadata is None
    assert acquisition._descriptor is None
    assert acquisition._descriptor_error is None
    if acquisition.event_bus:
        states = [e.data for e in acquisition.event_bus.events if e.type is EventType.AcquisitionState]
        assert states
        assert not states[-1].connected
        assert not states[-1].streaming
        assert states[-1].connection_id is None


@pytest.mark.parametrize("projected", [False, True])
def test_fetch_transport_loss_invalidates_identity_and_metadata(projected):
    async def scenario():
        source = Source()
        source.is_streaming = True

        async def read_chunk():
            source.is_connected = source.is_streaming = False
            raise asyncio.IncompleteReadError(b"partial", 20)

        source.read_chunk = read_chunk
        acquisition = runtime(source, projected=projected)
        await acquisition.fetch()

        assert_disconnected(acquisition)
        assert "IncompleteReadError" in acquisition._fetch_failure

    asyncio.run(scenario())


@pytest.mark.parametrize("command", [
    AcquisitionCommand.get_metadata,
    AcquisitionCommand.start_stream,
    AcquisitionCommand.stop_stream,
])
@pytest.mark.parametrize("correlated", [False, True])
def test_command_transport_failure_publishes_disconnection_before_failure(command, correlated):
    async def scenario():
        source = Source()
        source.is_streaming = command is AcquisitionCommand.stop_stream

        async def lose_connection():
            source.is_connected = source.is_streaming = False
            raise ConnectionResetError("peer closed during command")

        setattr(source, command.name, lose_connection)
        acquisition = runtime(source)
        # Force the metadata command to use the source, including without a request.
        acquisition._raw_metadata = None
        item = (command, CommandRequest("failed-command", {"refresh": True})) if correlated else command
        if correlated:
            await acquisition.execute(item)
            state_event, result_event = acquisition.event_bus.events[-2:]
            assert state_event.type is EventType.AcquisitionState
            assert result_event.type is EventType.CommandResult
            assert result_event.data.error_code == "ConnectionResetError"
            assert result_event.data.failure_kind is CommandFailureKind.EXECUTION_FAILED
        else:
            with pytest.raises(ConnectionResetError, match="peer closed"):
                await acquisition.execute(item)
        assert_disconnected(acquisition)

    asyncio.run(scenario())


def test_metadata_timeout_keeps_connection_and_is_not_continuity_loss():
    async def scenario():
        source = Source()

        async def timeout():
            raise TimeoutError("metadata reply delayed")

        source.get_metadata = timeout
        acquisition = runtime(source)
        connection_id = acquisition.connection_id
        await acquisition.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata", {"refresh": True})))

        state, result = acquisition.event_bus.events[-2:]
        assert state.data.connected
        assert state.data.connection_id == connection_id
        assert result.data.error_code == "SOURCE_METADATA_TIMEOUT"
        assert result.data.failure_kind is CommandFailureKind.REJECTED
        assert acquisition._fetch_failure is None

    asyncio.run(scenario())


class DelayedTeardownSource(Source):
    def __init__(self):
        super().__init__()
        self.is_streaming = True
        self.teardown_started = asyncio.Event()
        self.release_teardown = asyncio.Event()

    async def read_chunk(self):
        self.is_connected = self.is_streaming = False
        raise ConnectionResetError("original peer failure")

    async def stop_stream(self):
        self.calls.append("stop_stream_started")
        self.teardown_started.set()
        await self.release_teardown.wait()
        self.is_streaming = False
        self.calls.append("stop_stream_finished")


async def start_failing_reader():
    source = DelayedTeardownSource()
    acquisition = runtime(source)
    acquisition.fetch_task = asyncio.create_task(acquisition.fetch())
    await asyncio.wait_for(source.teardown_started.wait(), timeout=1)
    return acquisition, source, acquisition.fetch_task


def test_connect_waits_for_old_teardown_without_losing_failure_or_stopping_new_connection():
    async def scenario():
        acquisition, source, old_task = await start_failing_reader()
        previous_id = acquisition.connection_id
        connecting = asyncio.create_task(acquisition.execute((AcquisitionCommand.connect, CommandRequest("connect"))))
        await asyncio.sleep(0)
        assert not connecting.done()
        assert source.calls == ["stop_stream_started"]

        source.release_teardown.set()
        await asyncio.wait_for(connecting, timeout=1)

        assert old_task.done() and not old_task.cancelled()
        assert acquisition.fetch_task is None
        assert source.calls == ["stop_stream_started", "stop_stream_finished", "connect"]
        assert source.is_connected
        assert acquisition.connection_id and acquisition.connection_id != previous_id
        assert acquisition._fetch_failure == "ConnectionResetError: original peer failure"
        assert acquisition.event_bus.events[-1].data.ok
        # A new socket cannot authorize resuming a stream whose data was lost.
        await acquisition.execute((AcquisitionCommand.start_stream, CommandRequest("read")))
        result = acquisition.event_bus.events[-1].data
        assert result.error_code == "ACQUISITION_STREAM_CONTINUITY_LOST"
        assert result.failure_kind is CommandFailureKind.REJECTED
        assert "Stop engine" in result.message and "Start engine" in result.message
        assert "Connect" in result.message and "Start reading" in result.message
        assert "start_stream" not in source.calls

    asyncio.run(scenario())


def test_connect_timeout_does_not_cancel_old_teardown_or_create_new_connection():
    async def scenario():
        acquisition, source, old_task = await start_failing_reader()
        acquisition.FETCH_FINISH_TIMEOUT_S = 0.01
        await acquisition.execute((AcquisitionCommand.connect, CommandRequest("connect")))

        result = acquisition.event_bus.events[-1].data
        assert result.error_code == "ACQUISITION_STOP_PENDING"
        assert result.failure_kind is CommandFailureKind.REJECTED
        assert acquisition.fetch_task is old_task
        assert not old_task.done() and not old_task.cancelling()
        assert source.calls == ["stop_stream_started"]
        assert_disconnected(acquisition)

        source.release_teardown.set()
        await asyncio.wait_for(old_task, timeout=1)
        await acquisition.execute((AcquisitionCommand.connect, CommandRequest("retry")))
        assert acquisition.event_bus.events[-1].data.ok
        assert source.calls[-1] == "connect"
        assert acquisition._fetch_failure == "ConnectionResetError: original peer failure"

    asyncio.run(scenario())


def test_cancelled_connect_does_not_cancel_old_teardown():
    async def scenario():
        acquisition, source, old_task = await start_failing_reader()
        connecting = asyncio.create_task(acquisition.execute((AcquisitionCommand.connect, CommandRequest("connect"))))
        await asyncio.sleep(0)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting

        assert acquisition.fetch_task is old_task
        assert not old_task.done() and not old_task.cancelling()
        assert "connect" not in source.calls
        assert_disconnected(acquisition)
        source.release_teardown.set()
        await asyncio.wait_for(old_task, timeout=1)
        assert acquisition._fetch_failure == "ConnectionResetError: original peer failure"

    asyncio.run(scenario())


def test_stop_write_failure_latches_loss_before_cancelling_pending_reader():
    async def scenario():
        source = Source()
        source.is_streaming = True
        reading = asyncio.Event()

        async def read_chunk():
            reading.set()
            await asyncio.Event().wait()

        async def broken_stop():
            source.is_connected = source.is_streaming = False
            raise BrokenPipeError("stop write failed")

        source.read_chunk = read_chunk
        source.stop_stream = broken_stop
        acquisition = runtime(source)
        acquisition.fetch_task = asyncio.create_task(acquisition.fetch())
        old_task = acquisition.fetch_task
        await asyncio.wait_for(reading.wait(), timeout=1)
        await acquisition.execute((AcquisitionCommand.stop_stream, CommandRequest("stop")))

        assert old_task.done()
        assert acquisition.fetch_task is None
        assert acquisition._fetch_failure == "BrokenPipeError: stop write failed"
        assert_disconnected(acquisition)
        assert acquisition.event_bus.events[-1].data.error_code == "BrokenPipeError"
        await acquisition.execute(AcquisitionCommand.disconnect)
        await acquisition.execute(AcquisitionCommand.connect)
        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain")))
        result = acquisition.event_bus.events[-1].data
        assert result.error_code == "ACQUISITION_STREAM_CONTINUITY_LOST"
        assert result.failure_kind is CommandFailureKind.REJECTED
        assert acquisition.data_manager.items == []

    asyncio.run(scenario())
