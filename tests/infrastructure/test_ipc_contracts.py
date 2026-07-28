"""
Tests for the stimulation IPC message contract (contracts.py).

Pins the wire shape and the protocol-version policy that both NetworkServer and
NetworkClient now share:
  - event ⇄ body round-trips as {"type": <int>, "data": ...};
  - encode_event stamps PROTOCOL_VERSION into the frame header;
  - body_to_event validates the version and drops unsupported / malformed /
    unknown-type frames instead of mis-parsing them.
"""
from __future__ import annotations

import json

import pytest

from omnibci.infrastructure.ipc import contracts
from omnibci.infrastructure.ipc.contracts import (
    InvalidEventBody,
    MAX_FRAME_BODY_BYTES,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    UnsupportedProtocolVersion,
    event_to_body,
    encode_event,
    body_to_event,
    decode_event_body,
    require_supported_version,
)
from omnibci.infrastructure.ipc.protocol import (
    Frame,
    FrameBodyTooLarge,
    InvalidFrameHeader,
)
from omnibci.shared.events import Event, EventType


class TestWireShape:
    def test_event_to_body_shape(self):
        body = event_to_body(Event(EventType.TrialCompleted, data={"x": 1}))
        payload = json.loads(body)
        assert payload == {"type": int(EventType.TrialCompleted), "data": {"x": 1}}

    def test_roundtrip_event_to_body_to_event(self):
        original = Event(EventType.StartStimTrial, data=5)
        event = body_to_event(event_to_body(original).encode(), PROTOCOL_VERSION)
        assert event.type == EventType.StartStimTrial
        assert event.data == 5

    def test_decoded_type_is_eventtype_not_int(self):
        event = body_to_event(event_to_body(Event(EventType.SessionEnded, 3)).encode(), PROTOCOL_VERSION)
        assert isinstance(event.type, EventType)


class TestFraming:
    def test_v1_header_wire_bytes_are_unchanged(self):
        header = Frame.Header(version=1, body_size=3).to_bytes()
        assert header == b"\x00\x00\x00\x01\x00\x00\x00\x03"

    def test_encode_event_stamps_protocol_version(self):
        frame = encode_event(Event(EventType.TrialCompleted, data=1))
        header = Frame.Header.from_bytes(frame[: Frame.Header.HEADER_SIZE])
        assert header.version == PROTOCOL_VERSION

    def test_encode_event_body_is_decodable(self):
        frame = encode_event(Event(EventType.TrialCompleted, data=1))
        header = Frame.Header.from_bytes(frame[: Frame.Header.HEADER_SIZE])
        body = frame[Frame.Header.HEADER_SIZE:]
        assert header.body_size == len(body)
        event = body_to_event(body, header.version)
        assert event.type == EventType.TrialCompleted and event.data == 1

    @pytest.mark.parametrize("raw", [b"", b"\x00" * 7, b"\x00" * 9, "not-bytes"])
    def test_header_requires_exactly_eight_bytes(self, raw):
        with pytest.raises(InvalidFrameHeader):
            Frame.Header.from_bytes(raw)

    def test_header_rejects_oversized_body_before_body_read(self):
        raw = (
            PROTOCOL_VERSION.to_bytes(4, "big")
            + (MAX_FRAME_BODY_BYTES + 1).to_bytes(4, "big")
        )
        with pytest.raises(FrameBodyTooLarge):
            Frame.Header.from_bytes(raw)

    def test_encoder_applies_same_body_limit(self):
        with pytest.raises(FrameBodyTooLarge):
            Frame("x" * (MAX_FRAME_BODY_BYTES + 1), version=PROTOCOL_VERSION)

    def test_frame_and_header_cannot_be_mutated_out_of_sync(self):
        frame = Frame("ok", version=PROTOCOL_VERSION)

        with pytest.raises(AttributeError):
            frame.body = "a different byte length"
        with pytest.raises(AttributeError):
            frame.header.body_size = 999
        with pytest.raises(AttributeError):
            frame.header = Frame.Header(PROTOCOL_VERSION, 999)

        encoded = frame.to_bytes()
        header = Frame.Header.from_bytes(encoded[: Frame.Header.HEADER_SIZE])
        assert header.body_size == len(encoded[Frame.Header.HEADER_SIZE:]) == 2


class TestVersionPolicy:
    def test_supported_versions_contains_protocol_version(self):
        assert PROTOCOL_VERSION in SUPPORTED_VERSIONS

    def test_unsupported_version_is_dropped(self):
        body = event_to_body(Event(EventType.TrialCompleted, 1)).encode()
        unsupported = max(SUPPORTED_VERSIONS) + 99
        assert body_to_event(body, unsupported) is None

    def test_strict_version_check_is_available_at_header_boundary(self):
        unsupported = max(SUPPORTED_VERSIONS) + 99
        with pytest.raises(UnsupportedProtocolVersion):
            require_supported_version(unsupported)


class TestRobustness:
    def test_malformed_json_dropped(self):
        assert body_to_event(b"not json", PROTOCOL_VERSION) is None

    def test_unknown_event_type_dropped(self):
        body = json.dumps({"type": 999999, "data": 1}).encode()
        assert body_to_event(body, PROTOCOL_VERSION) is None

    def test_missing_type_dropped(self):
        body = json.dumps({"data": 1}).encode()
        assert body_to_event(body, PROTOCOL_VERSION) is None

    @pytest.mark.parametrize(
        "body",
        [
            b"not json",
            b"[]",
            json.dumps({"data": 1}).encode(),
            json.dumps({"type": 999999, "data": 1}).encode(),
            json.dumps({"type": True, "data": 1}).encode(),
        ],
    )
    def test_strict_stream_decoder_rejects_invalid_event_envelopes(self, body):
        with pytest.raises(InvalidEventBody):
            decode_event_body(body, PROTOCOL_VERSION)
