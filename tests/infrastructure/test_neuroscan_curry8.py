"""Protocol and lifecycle tests for the CURRY 8 NetStreaming driver."""

from __future__ import annotations

from pathlib import Path
import asyncio
import json
import struct
from unittest.mock import AsyncMock, MagicMock, call

import numpy as np
import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import (
    BlockType,
    ClientRequest,
    ControlCode,
    Curry8DataReader,
    Curry8Header,
    Curry8Metadata,
    Curry8ProtocolError,
    DataCode,
    InfoType,
)


_HEADER = struct.Struct(">4sHHIII")
_BASIC_INFO = struct.Struct("<IiiiII")


def _packet(
    identifier: bytes,
    code: int,
    request: int,
    body: bytes = b"",
    *,
    start_sample: int = 0,
    uncompressed_size: int = 0,
) -> bytes:
    return _HEADER.pack(
        identifier,
        code,
        request,
        start_sample,
        len(body),
        uncompressed_size,
    ) + body


def _channel_info(labels: tuple[str, ...]) -> bytes:
    records = []
    for channel_id, label in enumerate(labels):
        record = bytearray(Curry8DataReader.CHANNEL_INFO_SIZE)
        struct.pack_into("<i", record, 0, channel_id)
        encoded_label = label.encode("utf-16-le")
        record[4:4 + len(encoded_label)] = encoded_label
        records.append(record)
    return b"".join(records)


def _event(event_type: int, start_sample: int, annotation: str) -> bytes:
    encoded_annotation = annotation.encode("utf-16-le")
    annotation_field = encoded_annotation.ljust(520, b"\x00")
    return struct.pack(
        "<iiii520s",
        event_type,
        0,
        start_sample,
        start_sample,
        annotation_field,
    )


async def _run_fake_server_scenario(*, compressed: bool = False):
    requests = []
    server_done = asyncio.get_running_loop().create_future()

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                raw_header = await reader.readexactly(_HEADER.size)
                header = Curry8Header.from_bytes(raw_header)
                requests.append(header.request)

                if header.request == ClientRequest.VERSION:
                    body = struct.pack("<i", 803)
                    writer.write(
                        _packet(b"DATA", DataCode.INFO, InfoType.VERSION, body)
                    )
                elif header.request == ClientRequest.BASIC_INFO:
                    body = _BASIC_INFO.pack(_BASIC_INFO.size, 4, 1000, 4, 1, 0)
                    writer.write(
                        _packet(b"DATA", DataCode.INFO, InfoType.BASIC_INFO, body)
                    )
                elif header.request == ClientRequest.CHANNEL_INFO:
                    body = _channel_info(("Fz", "Cz", "Pz", "Trigger"))
                    writer.write(
                        _packet(b"DATA", DataCode.INFO, InfoType.CHANNEL_INFO, body)
                    )
                elif header.request == ClientRequest.STREAMING_START:
                    if compressed:
                        writer.write(
                            _packet(
                                b"DATA",
                                DataCode.EEG,
                                BlockType.FLOAT32_ZIP,
                                b"compressed",
                                start_sample=100,
                                uncompressed_size=48,
                            )
                        )
                    else:
                        first_eeg = np.asarray(
                            [
                                [1.0, 10.0, 100.0, 0.0],
                                [2.0, 20.0, 200.0, 7.0],
                                [3.0, 30.0, 300.0, 0.0],
                            ],
                            dtype="<f4",
                        ).tobytes()
                        second_eeg = np.asarray(
                            [[4.0, 40.0, 400.0, 9.0]],
                            dtype="<f4",
                        ).tobytes()
                        writer.write(
                            _packet(
                                b"DATA",
                                DataCode.EEG,
                                BlockType.FLOAT32,
                                first_eeg,
                                start_sample=100,
                            )
                        )
                        writer.write(
                            _packet(
                                b"DATA",
                                DataCode.EVENTS,
                                BlockType.EVENT_LIST,
                                _event(99, 103, "annotation"),
                            )
                        )
                        writer.write(
                            _packet(
                                b"DATA",
                                DataCode.EEG,
                                BlockType.FLOAT32,
                                second_eeg,
                                start_sample=103,
                            )
                        )
                elif header.request == ClientRequest.STREAMING_STOP:
                    break
                else:
                    raise AssertionError(f"Unexpected CURRY request {header.request}")
                await writer.drain()
        except BaseException as exc:
            if not server_done.done():
                server_done.set_exception(exc)
        else:
            server_done.set_result(None)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    driver = Curry8DataReader(parameters={'DATA_PROVIDER_IP': '127.0.0.1', 'DATA_PROVIDER_PORT': port, 'timeout': 1.0}, resource_root=Path(__file__).parent)

    try:
        assert await driver.connect() is None
        metadata = await driver.observe()
        assert isinstance(metadata.native_text, str)
        assert json.loads(metadata.native_text) == {
            "protocol_version": 803,
            "channel_count": 4,
            "sample_rate": 1000,
            "data_size": 4,
            "allow_amplifier_control": True,
            "allow_recording_control": False,
            "channel_labels": ["Fz", "Cz", "Pz", "Trigger"],
        }
        descriptor = metadata.signal_info
        assert descriptor.signal_channel_count == 3
        assert descriptor.output_row_count == 4
        assert descriptor.trigger_row_index == 3
        assert metadata.native_event == "final-row"
        assert [channel.name for channel in descriptor.channels] == ["Fz", "Cz", "Pz"]
        assert await driver.start_stream() is None

        if compressed:
            with pytest.raises(Curry8ProtocolError, match="Compressed"):
                await driver.read_chunk()
            chunk = None
        else:
            chunk = await asyncio.wait_for(driver.read_chunk(), timeout=1.0)
            np.testing.assert_array_equal(
                await asyncio.wait_for(driver.read_chunk(), timeout=1.0),
                np.asarray([[4.0], [40.0], [400.0], [9.0]], dtype=np.float32),
            )

        assert await driver.stop_stream() is None
        assert await driver.disconnect() is None
        await asyncio.wait_for(server_done, timeout=1.0)
    finally:
        if driver._is_connected:
            await driver.disconnect()
        server.close()
        await server.wait_closed()

    return chunk, requests


def test_header_uses_the_20_byte_network_order_layout():
    header = Curry8Header(
        identifier=b"CTRL",
        code=ControlCode.FROM_CLIENT,
        request=ClientRequest.STREAMING_START,
    )

    assert header.to_bytes() == bytes.fromhex(
        "43 54 52 4c 00 02 00 08 00 00 00 00 00 00 00 00 00 00 00 00"
    )
    assert Curry8Header.from_bytes(header.to_bytes()) == header


def test_curry_host_port_timeout_are_explicit_parameters():
    reader = Curry8DataReader(
        parameters={"DATA_PROVIDER_IP": "192.0.2.4", "DATA_PROVIDER_PORT": 5678, "timeout": 0.25},
        resource_root=Path(__file__).parent,
    )
    default_reader = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)

    assert (reader.data_provider_ip, reader.data_provider_port, reader.timeout) == ("192.0.2.4", 5678, 0.25)
    assert reader.reader is None and reader.writer is None
    assert (default_reader.data_provider_ip, default_reader.data_provider_port) == ("127.0.0.1", 4455)
    with pytest.raises(TypeError, match="parameters"):
        Curry8DataReader()


@pytest.mark.parametrize("timeout", [0, -1, True, None, "5", float("nan"), float("inf")])
def test_curry_rejects_invalid_network_timeout_before_connecting(timeout):
    with pytest.raises(ValueError, match="timeout must be positive"):
        Curry8DataReader(parameters={"timeout": timeout}, resource_root=Path(__file__).parent)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_version,data_size,dtype,raw_events", [
    (802, 2, "<i2", [0, 1, 7, 7, 255, 0]),
    (802, 2, "<i2", [-256, -255, -249, -249, -1, -256]),
    (803, 4, "<f4", [0, 1, 7, 7, 255, 0]),
    (803, 4, "<f4", [65280, 65281, 65287, 65287, 65535, 65280]),
])
async def test_single_data_packet_extracts_low_byte_triggers_without_waiting_for_next_block(
    protocol_version, data_size, dtype, raw_events,
):
    driver = Curry8DataReader(parameters={'DATA_PROVIDER_IP': '127.0.0.1', 'DATA_PROVIDER_PORT': 4455}, resource_root=Path(__file__).parent)
    driver._is_connected = driver._is_streaming = True
    driver._metadata = Curry8Metadata(
        protocol_version=protocol_version,
        channel_count=3,
        sample_rate=500,
        data_size=data_size,
        allow_amplifier_control=False,
        allow_recording_control=False,
        channel_labels=("C3", "C4", "Trigger"),
    )
    driver.reader = asyncio.StreamReader()
    expected = np.asarray([
        [-1.5, 2.25, -3.5, 4.25, -5.5, 6.25],
        [10, -20, 30, -40, 50, -60],
        [0, 1, 7, 7, 255, 0],
    ], dtype=dtype)
    frames = expected.T.copy()
    frames[:, -1] = raw_events
    driver.reader.feed_data(_packet(b"DATA", DataCode.EEG, BlockType.FLOAT32, frames.tobytes()))
    driver.reader.feed_eof()

    chunk = await driver.read_chunk()

    np.testing.assert_array_equal(chunk, expected)
    assert chunk.dtype == np.dtype(dtype)


@pytest.mark.asyncio
async def test_metadata_requeries_the_stopped_device_and_keeps_event_rows_separate():
    driver = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    driver._is_connected = True
    driver._protocol_version = 803
    driver._request_info = AsyncMock(side_effect=[
        _BASIC_INFO.pack(_BASIC_INFO.size, 4, 1000, 4, 1, 0),
        _channel_info(("Fz", "Cz", "Pz", "Trigger")),
        _BASIC_INFO.pack(_BASIC_INFO.size, 3, 500, 4, 1, 0),
        _channel_info(("C3", "C4", "Trigger")),
    ])

    first = await driver.observe()
    first_descriptor = first.signal_info
    refreshed = await driver.observe()
    refreshed_descriptor = refreshed.signal_info

    assert json.loads(first.native_text)["channel_count"] == first_descriptor.output_row_count == 4
    assert first_descriptor.signal_channel_count == 3
    assert json.loads(refreshed.native_text)["sample_rate"] == refreshed_descriptor.sample_rate == 500
    assert json.loads(refreshed.native_text)["channel_count"] == refreshed_descriptor.output_row_count == 3
    assert refreshed_descriptor.signal_channel_count == 2
    assert json.loads(refreshed.native_text)["channel_labels"] == ["C3", "C4", "Trigger"]
    assert [channel.name for channel in refreshed_descriptor.channels] == ["C3", "C4"]
    assert refreshed_descriptor.trigger_row_index == 2
    assert refreshed.native_event == "final-row"
    assert "signal_channel_count" not in refreshed.native_text
    assert "native_event" not in refreshed.native_text
    assert "provenance" not in refreshed.native_text
    assert driver._request_info.await_args_list == [
        call(ClientRequest.BASIC_INFO, InfoType.BASIC_INFO),
        call(ClientRequest.CHANNEL_INFO, InfoType.CHANNEL_INFO),
    ] * 2

    driver._is_streaming = True
    with pytest.raises(RuntimeError, match="Stop the CURRY stream"):
        await driver.observe()
    assert driver._request_info.await_count == 4


@pytest.mark.asyncio
async def test_failed_observation_clears_previous_device_metadata():
    driver = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    driver._is_connected = True
    driver._protocol_version = 803
    driver._request_info = AsyncMock(side_effect=[
        _BASIC_INFO.pack(_BASIC_INFO.size, 3, 500, 4, 1, 0),
        _channel_info(("C3", "C4", "Trigger")),
        Curry8ProtocolError("Invalid device reply"),
    ])
    observation = await driver.observe()
    descriptor = observation.signal_info
    assert descriptor.signal_channel_count == 2
    assert driver._request_info.await_count == 2

    with pytest.raises(Curry8ProtocolError, match="Invalid device reply"):
        await driver.observe()
    assert driver._metadata is None
    assert driver._request_info.await_count == 3


@pytest.mark.asyncio
async def test_metadata_without_a_streaming_cache_never_reads_the_stream_socket():
    driver = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    driver._is_connected = driver._is_streaming = True
    driver._request_info = AsyncMock()

    with pytest.raises(RuntimeError, match="Stop the CURRY stream"):
        await driver.observe()

    driver._request_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_metadata_skips_queued_stream_packets():
    driver = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    driver._is_connected = driver._is_streaming = True
    driver._protocol_version = 803
    driver.reader = asyncio.StreamReader()
    driver.writer = MagicMock(drain=AsyncMock())
    driver.reader.feed_data(b"".join([
        _packet(b"DATA", DataCode.EEG, BlockType.FLOAT32, np.zeros((2, 3), dtype="<f4").tobytes()),
        _packet(b"DATA", DataCode.EVENTS, BlockType.EVENT_LIST, _event(7, 1, "target")),
        _packet(b"DATA", DataCode.IMPEDANCES, BlockType.FLOAT32, np.zeros(3, dtype="<f4").tobytes()),
        _packet(b"DATA", DataCode.INFO, InfoType.BASIC_INFO,
                _BASIC_INFO.pack(_BASIC_INFO.size, 3, 1000, 4, 1, 0)),
        _packet(b"DATA", DataCode.INFO, InfoType.CHANNEL_INFO, _channel_info(("Fz", "Cz", "Trigger"))),
    ]))

    await driver.stop_stream()
    metadata = await driver.observe()

    assert json.loads(metadata.native_text)["channel_count"] == 3
    assert json.loads(metadata.native_text)["sample_rate"] == 1000
    assert json.loads(metadata.native_text)["channel_labels"] == ["Fz", "Cz", "Trigger"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message", [(99, "Unexpected packet"), (DataCode.EEG, "Timed out")])
async def test_stopped_metadata_rejects_unexpected_or_missing_reply(code, message):
    driver = Curry8DataReader(parameters={'timeout': 0.01}, resource_root=Path(__file__).parent)
    driver._is_connected = True
    driver.reader = asyncio.StreamReader()
    driver.writer = MagicMock(drain=AsyncMock())
    driver.reader.feed_data(_packet(b"DATA", code, BlockType.FLOAT32, np.zeros(3, dtype="<f4").tobytes()))

    with pytest.raises(Curry8ProtocolError, match=message):
        await driver.observe()


def test_stream_lifecycle_preserves_native_trigger_row_and_ignores_event_packets():
    chunk, requests = asyncio.run(_run_fake_server_scenario())

    np.testing.assert_array_equal(
        chunk,
        np.asarray(
            [
                [1.0, 2.0, 3.0],
                [10.0, 20.0, 30.0],
                [100.0, 200.0, 300.0],
                [0.0, 7.0, 0.0],
            ],
            dtype=np.float32,
        ),
    )
    assert requests == [
        ClientRequest.VERSION,
        ClientRequest.BASIC_INFO,
        ClientRequest.CHANNEL_INFO,
        ClientRequest.STREAMING_START,
        ClientRequest.STREAMING_STOP,
    ]


def test_compressed_stream_is_rejected_with_configuration_guidance():
    chunk, requests = asyncio.run(_run_fake_server_scenario(compressed=True))

    assert chunk is None
    assert requests[-2:] == [
        ClientRequest.STREAMING_START,
        ClientRequest.STREAMING_STOP,
    ]


def _connected_driver(*, streaming=True):
    from unittest.mock import Mock

    driver = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    driver.reader = asyncio.StreamReader()
    driver.writer = Mock(spec=asyncio.StreamWriter)
    driver.writer.drain = AsyncMock()
    driver.writer.wait_closed = AsyncMock()
    driver._is_connected = True
    driver._is_streaming = streaming
    driver._protocol_version = 803
    driver._metadata = Curry8Metadata(803, 2, 1000, 4, False, False, ("Cz", "Trigger"))
    return driver


def _assert_detached(driver):
    assert not driver._is_connected
    assert not driver._is_streaming
    assert driver.reader is None
    assert driver.writer is None
    assert driver._metadata is None
    assert driver._protocol_version is None
    assert driver._pending_header is None


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [b"", b"DATA", "body"])
async def test_eof_at_header_or_body_invalidates_connection_immediately(partial):
    driver = _connected_driver()
    reader, writer = driver.reader, driver.writer
    if partial == "body":
        reader.feed_data(Curry8Header(b"DATA", DataCode.EEG, BlockType.FLOAT32, body_size=8).to_bytes() + b"1234")
    else:
        reader.feed_data(partial)
    reader.feed_eof()

    read = asyncio.create_task(driver.read_chunk())
    await asyncio.sleep(0)
    assert read.done()
    with pytest.raises(asyncio.IncompleteReadError):
        await read
    _assert_detached(driver)
    writer.transport.abort.assert_called_once_with()
    writer.wait_closed.assert_not_awaited()


import errno


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    ConnectionResetError("reset"),
    ConnectionAbortedError("aborted"),
    OSError(errno.ENOTCONN, "not connected"),
    OSError(errno.EBADF, "closed socket"),
])
async def test_socket_read_failure_preserves_original_exception(error):
    driver = _connected_driver()
    writer = driver.writer
    driver.reader.set_exception(error)
    with pytest.raises(type(error)) as caught:
        await driver.read_chunk()
    assert caught.value is error
    _assert_detached(driver)
    writer.transport.abort.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start_stream", "stop_stream", "observe"])
@pytest.mark.parametrize("failure_at", ["write", "drain"])
async def test_send_failure_invalidates_lifecycle_and_metadata_commands(operation, failure_at):
    driver = _connected_driver(streaming=operation == "stop_stream")
    writer = driver.writer
    error = BrokenPipeError("peer closed")
    getattr(writer, failure_at).side_effect = error
    with pytest.raises(BrokenPipeError) as caught:
        await getattr(driver, operation)()
    assert caught.value is error
    _assert_detached(driver)
    writer.transport.abort.assert_called_once_with()
    writer.wait_closed.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_response_eof_discards_previous_device_details():
    driver = _connected_driver(streaming=False)
    driver.reader.feed_eof()
    with pytest.raises(asyncio.IncompleteReadError):
        await driver.observe()
    _assert_detached(driver)
    with pytest.raises(RuntimeError, match="metadata"):
        await driver.observe()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("temporary timeout"), asyncio.CancelledError(), OSError(11, "try again")])
@pytest.mark.parametrize("operation", ["read", "send"])
async def test_nonterminal_io_errors_do_not_invalidate_connection(error, operation):
    driver = _connected_driver()
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
    assert driver._is_connected and driver._is_streaming
    assert driver.reader is reader and driver.writer is writer
    assert driver._metadata is metadata
    assert driver._protocol_version == 803
    writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_protocol_data_error_does_not_mark_transport_disconnected():
    driver = _connected_driver()
    writer = driver.writer
    driver.reader.feed_data(Curry8Header(b"DATA", DataCode.EEG, BlockType.FLOAT32_ZIP).to_bytes())
    with pytest.raises(Curry8ProtocolError, match="Compressed"):
        await driver.read_chunk()
    assert driver._is_connected and driver._is_streaming
    assert driver._metadata is not None
    writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "send"])
async def test_old_transport_failure_does_not_clear_new_connection(operation):
    driver = _connected_driver()
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

    fresh = _connected_driver()
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
    assert driver._is_connected and driver._is_streaming
    assert driver.reader is fresh.reader and driver.writer is fresh.writer
    assert driver._metadata is fresh._metadata and driver._protocol_version == 803
    assert driver._pending_header is pending
    old_writer.transport.abort.assert_called_once_with()
    fresh.writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_slow_close_clears_state_before_wait_and_preserves_new_connection():
    driver = _connected_driver(streaming=False)
    old_writer = driver.writer
    finish_close = asyncio.Event()
    old_writer.wait_closed.side_effect = finish_close.wait
    task = asyncio.create_task(driver.disconnect())
    await asyncio.sleep(0)
    assert not task.done()
    _assert_detached(driver)

    fresh = _connected_driver()
    driver.reader, driver.writer = fresh.reader, fresh.writer
    driver._is_connected = driver._is_streaming = True
    driver._metadata, driver._protocol_version = fresh._metadata, 803
    finish_close.set()
    assert await task is None
    assert driver._is_connected and driver._is_streaming
    assert driver.reader is fresh.reader and driver.writer is fresh.writer
    assert driver._metadata is fresh._metadata and driver._protocol_version == 803


@pytest.mark.asyncio
async def test_disconnect_does_not_wait_forever_for_socket_close():
    driver = _connected_driver(streaming=False)
    driver.CLOSE_TIMEOUT = 0.01
    writer = driver.writer
    writer.wait_closed.side_effect = asyncio.Event().wait
    assert await asyncio.wait_for(driver.disconnect(), timeout=0.5) is None
    _assert_detached(driver)
    writer.close.assert_called_once_with()
    writer.transport.abort.assert_called_once_with()


@pytest.mark.asyncio
async def test_abort_failure_does_not_hide_original_read_failure():
    driver = _connected_driver()
    error = ConnectionResetError("original failure")
    driver.reader.set_exception(error)
    driver.writer.transport.abort.side_effect = OSError("abort failed")
    with pytest.raises(ConnectionResetError) as caught:
        await driver.read_chunk()
    assert caught.value is error
    _assert_detached(driver)


@pytest.mark.asyncio
async def test_metadata_timeout_keeps_the_existing_transport_connected():
    driver = _connected_driver(streaming=False)
    driver.timeout = 0.01
    reader, writer = driver.reader, driver.writer
    with pytest.raises(Curry8ProtocolError, match="Timed out"):
        await driver.observe()
    assert driver._is_connected
    assert driver.reader is reader and driver.writer is writer
    assert driver._protocol_version == 803
    writer.transport.abort.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_close_keeps_state_cleared_and_preserves_cancellation():
    driver = _connected_driver(streaming=False)
    writer = driver.writer
    writer.wait_closed.side_effect = asyncio.Event().wait
    task = asyncio.create_task(driver.disconnect())
    await asyncio.sleep(0)
    _assert_detached(driver)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_detached(driver)
    writer.close.assert_called_once_with()
    assert await driver.disconnect() is None


@pytest.mark.asyncio
async def test_close_timeout_and_abort_failure_still_allow_idempotent_disconnect():
    driver = _connected_driver(streaming=False)
    driver.CLOSE_TIMEOUT = 0.01
    writer = driver.writer
    writer.wait_closed.side_effect = asyncio.Event().wait
    writer.transport.abort.side_effect = OSError("abort failed")
    assert await asyncio.wait_for(driver.disconnect(), timeout=0.5) is None
    _assert_detached(driver)
    assert await driver.disconnect() is None
    writer.close.assert_called_once_with()
    writer.transport.abort.assert_called_once_with()
