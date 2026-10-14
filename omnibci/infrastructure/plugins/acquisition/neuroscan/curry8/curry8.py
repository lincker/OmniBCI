#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-08-19
"""Neuroscan CURRY 8 NetStreaming signal source."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import asdict, dataclass
from enum import IntEnum

import numpy as np

from omnibci.domain.ports.acquisition import SignalSource
from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor
from omnibci.infrastructure.acquisition.metadata import acquisition_descriptor
from omnibci.sdk import PluginContext, PluginParameters

logger = logging.getLogger(__name__)


class Curry8ProtocolError(RuntimeError):
    """The NetStreaming peer sent an unsupported or malformed packet."""


class ControlCode(IntEnum):
    FROM_SERVER = 1
    FROM_CLIENT = 2


class ServerRequest(IntEnum):
    ACQUISITION_START = 1
    ACQUISITION_STOP = 2


class ClientRequest(IntEnum):
    VERSION = 1
    CHANNEL_INFO = 3
    BASIC_INFO = 6
    STREAMING_START = 8
    STREAMING_STOP = 9


class DataCode(IntEnum):
    INFO = 1
    EEG = 2
    EVENTS = 3
    IMPEDANCES = 4


class InfoType(IntEnum):
    VERSION = 1
    BASIC_INFO = 2
    CHANNEL_INFO = 4


class BlockType(IntEnum):
    FLOAT32 = 1
    FLOAT32_ZIP = 2
    EVENT_LIST = 3


_HEADER_STRUCT = struct.Struct(">4sHHIII")
_BASIC_INFO_STRUCT = struct.Struct("<IiiiII")
_EVENT_STRUCT = struct.Struct("<iiii520s")


@dataclass(frozen=True, slots=True)
class Curry8Header:
    identifier: bytes
    code: int
    request: int
    start_sample: int = 0
    body_size: int = 0
    uncompressed_body_size: int = 0

    SIZE = _HEADER_STRUCT.size

    def to_bytes(self) -> bytes:
        if len(self.identifier) != 4:
            raise ValueError("NetStreaming packet identifiers must be four bytes")
        return _HEADER_STRUCT.pack(
            self.identifier,
            self.code,
            self.request,
            self.start_sample,
            self.body_size,
            self.uncompressed_body_size,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> Curry8Header:
        if len(raw) != cls.SIZE:
            raise Curry8ProtocolError(
                f"NetStreaming header must be {cls.SIZE} bytes, got {len(raw)}"
            )
        header = cls(*_HEADER_STRUCT.unpack(raw))
        if header.identifier not in (b"CTRL", b"DATA"):
            raise Curry8ProtocolError(
                f"Unknown NetStreaming packet identifier {header.identifier!r}"
            )
        if header.code <= 0 or header.request <= 0:
            raise Curry8ProtocolError(
                f"Invalid NetStreaming code/request {header.code}/{header.request}"
            )
        return header


@dataclass(frozen=True, slots=True)
class Curry8BasicInfo:
    channel_count: int
    sample_rate: int
    data_size: int
    allow_amplifier_control: bool
    allow_recording_control: bool

    @classmethod
    def from_bytes(cls, raw: bytes) -> Curry8BasicInfo:
        if len(raw) != _BASIC_INFO_STRUCT.size:
            raise Curry8ProtocolError(
                f"CURRY basic info must be {_BASIC_INFO_STRUCT.size} bytes, "
                f"got {len(raw)}"
            )
        size, channel_count, sample_rate, data_size, allow_amp, allow_recording = (
            _BASIC_INFO_STRUCT.unpack(raw)
        )
        if size != _BASIC_INFO_STRUCT.size:
            raise Curry8ProtocolError(f"Unexpected CURRY basic info size {size}")
        if channel_count <= 0 or channel_count >= 4000 or sample_rate <= 0:
            raise Curry8ProtocolError(
                "CURRY amplifier is not running or returned invalid acquisition info"
            )
        if data_size not in (2, 4):
            raise Curry8ProtocolError(f"Unsupported CURRY sample size {data_size}")
        return cls(
            channel_count=channel_count,
            sample_rate=sample_rate,
            data_size=data_size,
            allow_amplifier_control=bool(allow_amp),
            allow_recording_control=bool(allow_recording),
        )


@dataclass(frozen=True, slots=True)
class Curry8Metadata:
    protocol_version: int
    channel_count: int
    sample_rate: int
    data_size: int
    allow_amplifier_control: bool
    allow_recording_control: bool
    channel_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Curry8Event:
    event_type: int
    start_sample: int

    SIZE = _EVENT_STRUCT.size

    @classmethod
    def from_bytes(cls, raw: bytes) -> Curry8Event:
        if len(raw) != cls.SIZE:
            raise Curry8ProtocolError(
                f"CURRY event must be {cls.SIZE} bytes, got {len(raw)}"
            )
        event_type, _, start_sample, _, _ = _EVENT_STRUCT.unpack(raw)
        return cls(
            event_type=event_type,
            start_sample=start_sample,
        )


class Curry8DataReader(SignalSource):
    """Receive uncompressed EEG and event packets from CURRY 8."""

    plugin_type = "neuroscan_curry8_reader"

    DEFAULT_PORT = 4455
    MAX_BODY_SIZE = 10 * 1024 * 1024
    CHANNEL_INFO_SIZE = 144
    SUPPORTED_PROTOCOL_VERSIONS = frozenset((802, 803))

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float = 5.0,
        *,
        parameters: PluginParameters,
        context: PluginContext | None = None,
    ) -> None:
        super().__init__()
        self.data_provider_ip = host or parameters.get("DATA_PROVIDER_IP", "127.0.0.1")
        self.data_provider_port = int(
            port if port is not None
            else parameters.get("DATA_PROVIDER_PORT", self.DEFAULT_PORT)
        )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pending_header: tuple[asyncio.StreamReader, Curry8Header] | None = None
        self._protocol_version: int | None = None
        self._metadata: Curry8Metadata | None = None
        self._pending_events: list[Curry8Event] = []
        self._buffered_eeg: tuple[int, np.ndarray] | None = None

    @property
    def resume_without_checkpoint_safe(self) -> bool:
        """NetStreaming starts live EEG delivery without a replay request."""
        return True

    async def connect(self) -> bool:
        if self._is_connected:
            return True
        self._protocol_version = None
        self._metadata = None
        self._pending_header = None
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.data_provider_ip,
                    self.data_provider_port,
                ),
                timeout=self.timeout,
            )
            version_raw = await self._request_info(
                ClientRequest.VERSION,
                InfoType.VERSION,
            )
            if len(version_raw) != 4:
                raise Curry8ProtocolError(
                    f"CURRY protocol version must be four bytes, got {len(version_raw)}"
                )
            self._protocol_version = struct.unpack("<i", version_raw)[0]
            if self._protocol_version not in self.SUPPORTED_PROTOCOL_VERSIONS:
                raise Curry8ProtocolError(
                    f"Unsupported CURRY NetStreaming protocol "
                    f"{self._protocol_version}; expected 802 or 803"
                )
        except BaseException:
            await self._close_writer()
            raise
        self._is_connected = True
        return True

    async def disconnect(self) -> bool:
        try:
            if self._is_streaming:
                await self.stop_stream()
        finally:
            await self._close_writer()
            self._is_connected = False
            self._is_streaming = False
            self._protocol_version = None
            self._metadata = None
            self._reset_stream_state()
        return True

    async def get_metadata(self) -> str:
        if not self._is_connected:
            raise RuntimeError("Connect to CURRY before requesting metadata")
        if not self._is_streaming:
            self._metadata = None
            await self._load_metadata()
        metadata = self._metadata
        if metadata is None:
            raise RuntimeError("Stop the CURRY stream before requesting new metadata")
        return json.dumps(asdict(metadata), ensure_ascii=False, indent=2)

    def describe_output(self, *, source_id: str, connection_id: str) -> AcquisitionDescriptor:
        """Describe the output using the last successful device query."""
        metadata = self._metadata
        if not self._is_connected or metadata is None:
            raise RuntimeError("Get CURRY metadata before describing its output")
        return acquisition_descriptor({
            "channel_count": metadata.channel_count,
            "output_channel_count": metadata.channel_count + 1,
            "sample_rate": metadata.sample_rate,
            "channel_labels": list(metadata.channel_labels),
            "native_event": "separate-event-packets",
            "provenance": "device-reported",
        }, source_id=source_id, connection_id=connection_id)

    async def start_stream(self) -> bool:
        if not self._is_connected:
            raise RuntimeError("Connect to CURRY before starting its stream")
        if self._is_streaming:
            return True
        if self._metadata is None:
            await self._load_metadata()
        self._reset_stream_state()
        await self._send_request(ClientRequest.STREAMING_START)
        self._is_streaming = True
        return True

    async def stop_stream(self) -> bool:
        if not self._is_streaming:
            return True
        try:
            if self.writer is not None:
                await self._send_request(ClientRequest.STREAMING_STOP)
        finally:
            self._is_streaming = False
            self._reset_stream_state()
        return True

    async def read_chunk(self) -> np.ndarray:
        if not self._is_streaming:
            raise RuntimeError("Start the CURRY stream before reading data")

        while True:
            header, body = await self._read_packet()
            if header.identifier == b"CTRL":
                self._handle_control(header)
                continue

            if header.code == DataCode.INFO:
                self._handle_info(header)
                continue
            if header.code == DataCode.EVENTS:
                self._pending_events.extend(self._parse_events(header, body))
                continue
            if header.code == DataCode.IMPEDANCES:
                logger.debug("Ignoring CURRY impedance data while streaming EEG")
                continue
            if header.code != DataCode.EEG:
                raise Curry8ProtocolError(f"Unsupported CURRY data code {header.code}")
            if header.request == BlockType.FLOAT32_ZIP:
                raise Curry8ProtocolError(
                    "Compressed CURRY NetStreaming data is unsupported; "
                    "disable 'Use Compressed Format' on the CURRY server"
                )
            if header.request != BlockType.FLOAT32:
                raise Curry8ProtocolError(
                    f"Unsupported CURRY EEG block type {header.request}"
                )

            next_block = (header.start_sample, self._parse_eeg(body))
            if self._buffered_eeg is None:
                self._buffered_eeg = next_block
                continue

            ready = self._buffered_eeg
            self._buffered_eeg = next_block
            return self._attach_events(*ready)

    async def _load_metadata(self) -> None:
        basic_raw = await self._request_info(
            ClientRequest.BASIC_INFO,
            InfoType.BASIC_INFO,
        )
        basic = Curry8BasicInfo.from_bytes(basic_raw)
        channel_raw = await self._request_info(
            ClientRequest.CHANNEL_INFO,
            InfoType.CHANNEL_INFO,
        )
        labels = self._parse_channel_labels(channel_raw, basic.channel_count)
        if self._protocol_version is None:
            raise Curry8ProtocolError("CURRY protocol version was not negotiated")
        self._metadata = Curry8Metadata(
            protocol_version=self._protocol_version,
            channel_count=basic.channel_count,
            sample_rate=basic.sample_rate,
            data_size=basic.data_size,
            allow_amplifier_control=basic.allow_amplifier_control,
            allow_recording_control=basic.allow_recording_control,
            channel_labels=labels,
        )

    async def _request_info(
        self,
        request: ClientRequest,
        expected_info: InfoType,
    ) -> bytes:
        await self._send_request(request)
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    header, body = await self._read_packet()
                    if header.identifier == b"CTRL":
                        self._handle_control(header)
                        continue
                    if (
                        header.code == DataCode.INFO
                        and header.request == expected_info
                    ):
                        return body
                    if (not self._is_streaming
                            and header.code in (DataCode.EEG, DataCode.EVENTS, DataCode.IMPEDANCES)):
                        # A stopped stream can leave complete data packets ahead of the reply.
                        continue
                    self._handle_info(header)
        except TimeoutError as exc:
            raise Curry8ProtocolError(
                f"Timed out waiting for CURRY info packet {expected_info.name}"
            ) from exc

    async def _send_request(self, request: ClientRequest) -> None:
        if self.writer is None:
            raise ConnectionError("CURRY NetStreaming socket is not connected")
        header = Curry8Header(
            identifier=b"CTRL",
            code=ControlCode.FROM_CLIENT,
            request=request,
        )
        self.writer.write(header.to_bytes())
        await self.writer.drain()

    async def _read_packet(self) -> tuple[Curry8Header, bytes]:
        reader = self.reader
        if reader is None:
            raise ConnectionError("CURRY NetStreaming socket is not connected")
        if self._pending_header is None or self._pending_header[0] is not reader:
            self._pending_header = None
            raw_header = await reader.readexactly(Curry8Header.SIZE)
            header = Curry8Header.from_bytes(raw_header)
            if header.body_size > self.MAX_BODY_SIZE:
                raise Curry8ProtocolError(
                    f"CURRY packet body is too large: {header.body_size} bytes"
                )
            # Retain framing until readexactly finishes, including cancellation.
            self._pending_header = (reader, header)
        else:
            header = self._pending_header[1]
        body = await reader.readexactly(header.body_size)
        self._pending_header = None
        return header, body

    def _handle_control(self, header: Curry8Header) -> None:
        if header.code != ControlCode.FROM_SERVER:
            return
        if header.request == ServerRequest.ACQUISITION_START:
            self._metadata = None
        elif header.request == ServerRequest.ACQUISITION_STOP:
            self._metadata = None

    @staticmethod
    def _handle_info(header: Curry8Header) -> None:
        if header.identifier != b"DATA" or header.code != DataCode.INFO:
            raise Curry8ProtocolError(
                f"Unexpected packet while waiting for CURRY metadata: "
                f"{header.identifier!r} {header.code}/{header.request}"
            )

    @classmethod
    def _parse_channel_labels(
        cls,
        body: bytes,
        channel_count: int,
    ) -> tuple[str, ...]:
        expected_size = channel_count * cls.CHANNEL_INFO_SIZE
        if len(body) != expected_size:
            raise Curry8ProtocolError(
                f"CURRY channel info must be {expected_size} bytes, got {len(body)}"
            )
        labels = []
        for offset in range(0, len(body), cls.CHANNEL_INFO_SIZE):
            label_raw = body[offset + 4:offset + 84]
            labels.append(
                label_raw.decode("utf-16-le", errors="replace").split("\x00", 1)[0]
            )
        return tuple(labels)

    def _parse_eeg(self, body: bytes) -> np.ndarray:
        metadata = self._metadata
        if metadata is None:
            raise Curry8ProtocolError("Received CURRY EEG data before metadata")
        # Protocol 802 used native int16 samples; 803 sends float32 EEG blocks.
        dtype = np.dtype(
            "<i2"
            if metadata.protocol_version == 802 and metadata.data_size == 2
            else "<f4"
        )
        sample_stride = metadata.channel_count * dtype.itemsize
        if not body or len(body) % sample_stride:
            raise Curry8ProtocolError(
                f"Invalid CURRY EEG body size {len(body)} for "
                f"{metadata.channel_count} channels"
            )
        samples = np.frombuffer(body, dtype=dtype)
        return samples.reshape(-1, metadata.channel_count).T.copy()

    @staticmethod
    def _parse_events(
        header: Curry8Header,
        body: bytes,
    ) -> list[Curry8Event]:
        if header.request != BlockType.EVENT_LIST:
            raise Curry8ProtocolError(
                f"Unsupported CURRY event block type {header.request}"
            )
        if not body or len(body) % Curry8Event.SIZE:
            raise Curry8ProtocolError(
                f"Invalid CURRY event body size {len(body)}"
            )
        return [
            Curry8Event.from_bytes(body[offset:offset + Curry8Event.SIZE])
            for offset in range(0, len(body), Curry8Event.SIZE)
        ]

    def _attach_events(self, start_sample: int, eeg: np.ndarray) -> np.ndarray:
        trigger = np.zeros(eeg.shape[1], dtype=np.float32)
        end_sample = start_sample + eeg.shape[1]
        remaining_events = []

        for event in self._pending_events:
            if event.start_sample >= end_sample:
                remaining_events.append(event)
                continue
            if event.start_sample < start_sample:
                logger.warning(
                    "Dropping late CURRY event %d at sample %d; current block starts at %d",
                    event.event_type,
                    event.start_sample,
                    start_sample,
                )
                continue
            index = event.start_sample - start_sample
            if trigger[index] != 0:
                logger.warning(
                    "Multiple CURRY events at sample %d; keeping the latest event",
                    event.start_sample,
                )
            trigger[index] = event.event_type

        self._pending_events = remaining_events
        return np.vstack((eeg, trigger))

    def _reset_stream_state(self) -> None:
        self._pending_events.clear()
        self._buffered_eeg = None

    async def _close_writer(self) -> None:
        writer = self.writer
        self._pending_header = None
        self.reader = None
        self.writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
