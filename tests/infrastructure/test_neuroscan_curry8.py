"""Protocol and lifecycle tests for the CURRY 8 NetStreaming driver."""

from __future__ import annotations

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
    driver = Curry8DataReader(host="127.0.0.1", port=port, timeout=1.0, parameters={})

    try:
        assert await driver.connect() is True
        metadata = await driver.get_metadata()
        assert isinstance(metadata, str)
        assert json.loads(metadata) == {
            "protocol_version": 803,
            "channel_count": 4,
            "sample_rate": 1000,
            "data_size": 4,
            "allow_amplifier_control": True,
            "allow_recording_control": False,
            "channel_labels": ["Fz", "Cz", "Pz", "Trigger"],
        }
        descriptor = driver.describe_output(source_id=driver.plugin_type, connection_id="connection")
        assert descriptor.signal_channel_count == 3
        assert descriptor.output_row_count == 4
        assert descriptor.trigger_row_index == 3
        assert descriptor.native_event == "final-row"
        assert [channel.name for channel in descriptor.channels] == ["Fz", "Cz", "Pz"]
        assert await driver.start_stream() is True

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

        assert await driver.stop_stream() is True
        assert await driver.disconnect() is True
        await asyncio.wait_for(server_done, timeout=1.0)
    finally:
        if driver.is_connected:
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
    driver = Curry8DataReader(host="127.0.0.1", port=4455, parameters={})
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
    driver = Curry8DataReader(parameters={})
    driver._is_connected = True
    driver._protocol_version = 803
    driver._request_info = AsyncMock(side_effect=[
        _BASIC_INFO.pack(_BASIC_INFO.size, 4, 1000, 4, 1, 0),
        _channel_info(("Fz", "Cz", "Pz", "Trigger")),
        _BASIC_INFO.pack(_BASIC_INFO.size, 3, 500, 4, 1, 0),
        _channel_info(("C3", "C4", "Trigger")),
    ])

    first = await driver.get_metadata()
    first_descriptor = driver.describe_output(source_id=driver.plugin_type, connection_id="connection")
    refreshed = await driver.get_metadata()
    refreshed_descriptor = driver.describe_output(source_id=driver.plugin_type, connection_id="connection")

    assert json.loads(first)["channel_count"] == first_descriptor.output_row_count == 4
    assert first_descriptor.signal_channel_count == 3
    assert json.loads(refreshed)["sample_rate"] == refreshed_descriptor.sample_rate == 500
    assert json.loads(refreshed)["channel_count"] == refreshed_descriptor.output_row_count == 3
    assert refreshed_descriptor.signal_channel_count == 2
    assert json.loads(refreshed)["channel_labels"] == ["C3", "C4", "Trigger"]
    assert [channel.name for channel in refreshed_descriptor.channels] == ["C3", "C4"]
    assert refreshed_descriptor.trigger_row_index == 2
    assert refreshed_descriptor.native_event == "final-row"
    assert "signal_channel_count" not in refreshed
    assert "native_event" not in refreshed
    assert "provenance" not in refreshed
    assert driver._request_info.await_args_list == [
        call(ClientRequest.BASIC_INFO, InfoType.BASIC_INFO),
        call(ClientRequest.CHANNEL_INFO, InfoType.CHANNEL_INFO),
    ] * 2

    driver._is_streaming = True
    assert await driver.get_metadata() == refreshed
    assert driver.describe_output(source_id=driver.plugin_type, connection_id="connection") == refreshed_descriptor
    assert driver._request_info.await_count == 4


@pytest.mark.asyncio
async def test_output_description_requires_successful_metadata_and_never_queries_socket():
    driver = Curry8DataReader(parameters={})
    driver._is_connected = True
    driver._protocol_version = 803
    driver._request_info = AsyncMock(side_effect=[
        _BASIC_INFO.pack(_BASIC_INFO.size, 3, 500, 4, 1, 0),
        _channel_info(("C3", "C4", "Trigger")),
        Curry8ProtocolError("Invalid device reply"),
    ])
    with pytest.raises(RuntimeError, match="Get CURRY metadata"):
        driver.describe_output(source_id=driver.plugin_type, connection_id="connection")
    driver._request_info.assert_not_awaited()

    await driver.get_metadata()
    descriptor = driver.describe_output(source_id=driver.plugin_type, connection_id="connection")
    assert descriptor.signal_channel_count == 2
    assert driver._request_info.await_count == 2

    with pytest.raises(Curry8ProtocolError, match="Invalid device reply"):
        await driver.get_metadata()
    with pytest.raises(RuntimeError, match="Get CURRY metadata"):
        driver.describe_output(source_id=driver.plugin_type, connection_id="connection")
    assert driver._request_info.await_count == 3


@pytest.mark.asyncio
async def test_metadata_without_a_streaming_cache_never_reads_the_stream_socket():
    driver = Curry8DataReader(parameters={})
    driver._is_connected = driver._is_streaming = True
    driver._request_info = AsyncMock()

    with pytest.raises(RuntimeError, match="Stop the CURRY stream"):
        await driver.get_metadata()

    driver._request_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_stopped_metadata_skips_queued_stream_packets():
    driver = Curry8DataReader(parameters={})
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
    metadata = await driver.get_metadata()

    assert json.loads(metadata)["channel_count"] == 3
    assert json.loads(metadata)["sample_rate"] == 1000
    assert json.loads(metadata)["channel_labels"] == ["Fz", "Cz", "Trigger"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message", [(99, "Unexpected packet"), (DataCode.EEG, "Timed out")])
async def test_stopped_metadata_rejects_unexpected_or_missing_reply(code, message):
    driver = Curry8DataReader(parameters={}, timeout=0.01)
    driver._is_connected = True
    driver.reader = asyncio.StreamReader()
    driver.writer = MagicMock(drain=AsyncMock())
    driver.reader.feed_data(_packet(b"DATA", code, BlockType.FLOAT32, np.zeros(3, dtype="<f4").tobytes()))

    with pytest.raises(Curry8ProtocolError, match=message):
        await driver.get_metadata()


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
