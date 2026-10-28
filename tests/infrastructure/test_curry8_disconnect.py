"""Terminal transport failures invalidate CURRY state without hiding their cause."""

import asyncio
import errno
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import (
    BlockType,
    ClientRequest,
    Curry8DataReader,
    Curry8Header,
    Curry8Metadata,
    Curry8ProtocolError,
    DataCode,
)


def connected_driver(*, streaming=True):
    driver = Curry8DataReader(parameters={})
    driver.reader = asyncio.StreamReader()
    driver.writer = Mock(spec=asyncio.StreamWriter)
    driver.writer.drain = AsyncMock()
    driver.writer.wait_closed = AsyncMock()
    driver._is_connected = True
    driver._is_streaming = streaming
    driver._protocol_version = 803
    driver._metadata = Curry8Metadata(803, 2, 1000, 4, False, False, ("Cz", "Trigger"))
    return driver


def assert_detached(driver):
    assert not driver.is_connected
    assert not driver.is_streaming
    assert driver.reader is None
    assert driver.writer is None
    assert driver._metadata is None
    assert driver._protocol_version is None
    assert driver._pending_header is None


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [b"", b"DATA", "body"])
async def test_eof_at_header_or_body_invalidates_connection_immediately(partial):
    driver = connected_driver()
    reader, writer = driver.reader, driver.writer
    if partial == "body":
        reader.feed_data(Curry8Header(b"DATA", DataCode.EEG, BlockType.FLOAT32, body_size=8).to_bytes() + b"1234")
    else:
        reader.feed_data(partial)
    reader.feed_eof()

    read = asyncio.create_task(driver.read_chunk())
    await asyncio.sleep(0)
    # There must be no close await between observing EOF and reporting failure.
    assert read.done()
    with pytest.raises(asyncio.IncompleteReadError):
        await read
    assert_detached(driver)
    writer.transport.abort.assert_called_once_with()
    writer.wait_closed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionResetError("reset"), ConnectionAbortedError("aborted"), OSError(errno.ENOTCONN, "not connected"), OSError(errno.EBADF, "closed socket")])
async def test_socket_read_failure_preserves_original_exception(error):
    driver = connected_driver()
    writer = driver.writer
    driver.reader.set_exception(error)
    with pytest.raises(type(error)) as caught:
        await driver.read_chunk()
    assert caught.value is error
    assert_detached(driver)
    writer.transport.abort.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start_stream", "stop_stream", "get_metadata"])
@pytest.mark.parametrize("failure_at", ["write", "drain"])
async def test_send_failure_invalidates_lifecycle_and_metadata_commands(operation, failure_at):
    driver = connected_driver(streaming=operation == "stop_stream")
    writer = driver.writer
    error = BrokenPipeError("peer closed")
    getattr(writer, failure_at).side_effect = error
    with pytest.raises(BrokenPipeError) as caught:
        await getattr(driver, operation)()
    assert caught.value is error
    assert_detached(driver)
    writer.transport.abort.assert_called_once_with()
    writer.wait_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_response_eof_discards_previous_device_details():
    driver = connected_driver(streaming=False)
    driver.reader.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await driver.get_metadata()
    assert_detached(driver)
    with pytest.raises(RuntimeError, match="metadata"):
        driver.describe_output(source_id="curry", connection_id="old")


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("temporary timeout"), asyncio.CancelledError(), OSError(errno.EAGAIN, "try again")])
@pytest.mark.parametrize("operation", ["read", "send"])
async def test_nonterminal_io_errors_do_not_invalidate_connection(error, operation):
    driver = connected_driver()
    reader, writer, metadata = driver.reader, driver.writer, driver._metadata
    if operation == "read":
        reader.set_exception(error)
        action = driver.read_chunk()
    else:
        writer.drain.side_effect = error
        action = driver._send_request(ClientRequest.STREAMING_START)
    with pytest.raises(type(error)) as caught:
        await action
    assert caught.value is error
    assert driver.is_connected and driver.is_streaming
    assert driver.reader is reader and driver.writer is writer
    assert driver._metadata is metadata
    assert driver._protocol_version == 803
    writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_protocol_data_error_does_not_mark_transport_disconnected():
    driver = connected_driver()
    writer = driver.writer
    driver.reader.feed_data(Curry8Header(b"DATA", DataCode.EEG, BlockType.FLOAT32_ZIP).to_bytes())
    with pytest.raises(Curry8ProtocolError, match="Compressed"):
        await driver.read_chunk()
    assert driver.is_connected and driver.is_streaming
    assert driver._metadata is not None
    writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "send"])
async def test_old_transport_failure_does_not_clear_new_connection(operation):
    driver = connected_driver()
    old_reader, old_writer = driver.reader, driver.writer
    pending_send = asyncio.get_running_loop().create_future()
    if operation == "read":
        action = driver.read_chunk()
    else:
        async def drain():
            await pending_send
        old_writer.drain.side_effect = drain
        action = driver._send_request(ClientRequest.STREAMING_START)
    task = asyncio.create_task(action)
    await asyncio.sleep(0)
    assert not task.done()

    fresh = connected_driver()
    driver.reader, driver.writer = fresh.reader, fresh.writer
    driver._metadata = fresh._metadata
    pending = (fresh.reader, Curry8Header(b"DATA", DataCode.EEG, BlockType.FLOAT32, body_size=8))
    driver._pending_header = pending
    if operation == "read":
        old_reader.feed_eof()
        failure_type = asyncio.IncompleteReadError
    else:
        pending_send.set_exception(BrokenPipeError("old writer failed"))
        failure_type = BrokenPipeError
    with pytest.raises(failure_type):
        await task
    assert driver.is_connected and driver.is_streaming
    assert driver.reader is fresh.reader and driver.writer is fresh.writer
    assert driver._metadata is fresh._metadata and driver._protocol_version == 803
    assert driver._pending_header is pending
    old_writer.transport.abort.assert_called_once_with()
    fresh.writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_slow_close_clears_state_before_wait_and_preserves_new_connection():
    driver = connected_driver(streaming=False)
    old_writer = driver.writer
    finish_close = asyncio.Event()
    old_writer.wait_closed.side_effect = finish_close.wait
    task = asyncio.create_task(driver.disconnect())
    await asyncio.sleep(0)
    assert not task.done()
    assert_detached(driver)

    fresh = connected_driver()
    driver.reader, driver.writer = fresh.reader, fresh.writer
    driver._is_connected = driver._is_streaming = True
    driver._metadata, driver._protocol_version = fresh._metadata, 803
    finish_close.set()
    assert await task is True
    assert driver.is_connected and driver.is_streaming
    assert driver.reader is fresh.reader and driver.writer is fresh.writer
    assert driver._metadata is fresh._metadata and driver._protocol_version == 803


@pytest.mark.asyncio
async def test_disconnect_does_not_wait_forever_for_socket_close():
    driver = connected_driver(streaming=False)
    driver.CLOSE_TIMEOUT = 0.01
    writer = driver.writer
    writer.wait_closed.side_effect = asyncio.Event().wait
    assert await asyncio.wait_for(driver.disconnect(), timeout=0.5) is True
    assert_detached(driver)
    writer.close.assert_called_once_with()
    writer.transport.abort.assert_called_once_with()


@pytest.mark.asyncio
async def test_abort_failure_does_not_hide_original_read_failure():
    driver = connected_driver()
    error = ConnectionResetError("original failure")
    driver.reader.set_exception(error)
    driver.writer.transport.abort.side_effect = OSError("abort failed")
    with pytest.raises(ConnectionResetError) as caught:
        await driver.read_chunk()
    assert caught.value is error
    assert_detached(driver)


@pytest.mark.asyncio
async def test_metadata_timeout_keeps_the_existing_transport_connected():
    driver = connected_driver(streaming=False)
    driver.timeout = 0.01
    reader, writer = driver.reader, driver.writer
    with pytest.raises(Curry8ProtocolError, match="Timed out"):
        await driver.get_metadata()
    assert driver.is_connected
    assert driver.reader is reader and driver.writer is writer
    assert driver._protocol_version == 803
    writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_state_cleared_and_preserves_cancellation():
    driver = connected_driver(streaming=False)
    writer = driver.writer
    writer.wait_closed.side_effect = asyncio.Event().wait
    task = asyncio.create_task(driver.disconnect())
    await asyncio.sleep(0)
    assert_detached(driver)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_detached(driver)
    writer.close.assert_called_once_with()
    assert await driver.disconnect() is True


@pytest.mark.asyncio
async def test_close_timeout_and_abort_failure_still_allow_idempotent_disconnect():
    driver = connected_driver(streaming=False)
    driver.CLOSE_TIMEOUT = 0.01
    writer = driver.writer
    writer.wait_closed.side_effect = asyncio.Event().wait
    writer.transport.abort.side_effect = OSError("abort failed")
    assert await asyncio.wait_for(driver.disconnect(), timeout=0.5) is True
    assert_detached(driver)
    assert await driver.disconnect() is True
    writer.close.assert_called_once_with()
    writer.transport.abort.assert_called_once_with()
