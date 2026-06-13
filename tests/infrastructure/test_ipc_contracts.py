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

from omnibci.infrastructure.ipc import contracts
from omnibci.infrastructure.ipc.contracts import (
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    event_to_body,
    encode_event,
    body_to_event,
)
from omnibci.infrastructure.ipc.protocol import Frame
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


class TestVersionPolicy:
    def test_supported_versions_contains_protocol_version(self):
        assert PROTOCOL_VERSION in SUPPORTED_VERSIONS

    def test_unsupported_version_is_dropped(self):
        body = event_to_body(Event(EventType.TrialCompleted, 1)).encode()
        unsupported = max(SUPPORTED_VERSIONS) + 99
        assert body_to_event(body, unsupported) is None


class TestRobustness:
    def test_malformed_json_dropped(self):
        assert body_to_event(b"not json", PROTOCOL_VERSION) is None

    def test_unknown_event_type_dropped(self):
        body = json.dumps({"type": 999999, "data": 1}).encode()
        assert body_to_event(body, PROTOCOL_VERSION) is None

    def test_missing_type_dropped(self):
        body = json.dumps({"data": 1}).encode()
        assert body_to_event(body, PROTOCOL_VERSION) is None
