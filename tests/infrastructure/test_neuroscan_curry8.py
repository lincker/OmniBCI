"""Protocol and lifecycle tests for the CURRY 8 NetStreaming driver."""

from __future__ import annotations

import asyncio
import struct

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
                    body = _BASIC_INFO.pack(_BASIC_INFO.size, 3, 1000, 4, 1, 0)
                    writer.write(
                        _packet(b"DATA", DataCode.INFO, InfoType.BASIC_INFO, body)
                    )
                elif header.request == ClientRequest.CHANNEL_INFO:
                    body = _channel_info(("Fz", "Cz", "Pz"))
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
                                uncompressed_size=36,
                            )
                        )
                    else:
                        first_eeg = np.asarray(
                            [
                                [1.0, 10.0, 100.0],
                                [2.0, 20.0, 200.0],
                                [3.0, 30.0, 300.0],
                            ],
                            dtype="<f4",
                        ).tobytes()
                        second_eeg = np.asarray(
                            [[4.0, 40.0, 400.0]],
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
                                _event(7, 101, "target"),
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
        assert metadata == {
            "device": "Neuroscan CURRY 8",
            "protocol_version": 803,
            "channel_count": 3,
            "output_channel_count": 4,
            "sample_rate": 1000,
            "data_size": 4,
            "allow_amplifier_control": True,
            "allow_recording_control": False,
            "channel_labels": ["Fz", "Cz", "Pz"],
        }
        assert await driver.start_stream() is True

        if compressed:
            with pytest.raises(Curry8ProtocolError, match="Compressed"):
                await driver.read_chunk()
            chunk = None
        else:
            chunk = await driver.read_chunk()

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


def test_protocol_802_native_int16_samples_are_supported():
    driver = Curry8DataReader(host="127.0.0.1", port=4455, parameters={})
    driver._metadata = Curry8Metadata(
        protocol_version=802,
        channel_count=2,
        sample_rate=500,
        data_size=2,
        allow_amplifier_control=False,
        allow_recording_control=False,
        channel_labels=("C3", "C4"),
    )

    packet = np.asarray([[1, 10], [2, 20]], dtype="<i2").tobytes()

    np.testing.assert_array_equal(
        driver._parse_eeg(packet),
        np.asarray([[1, 2], [10, 20]], dtype=np.int16),
    )


def test_stream_lifecycle_decodes_eeg_and_aligns_events():
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
