"""
Shared IPC Event message contract.

This is the single source of truth for the *wire shape* and *protocol version*
of every server IPC ↔ output peer socket channel.
Both directions encode/decode through here, so the format cannot drift between
ends — the class of bug that previously made a channel one-directional
(``event_type`` vs ``type``, ``to_dict`` vs ``to_json``).

Wire format:
    Frame header: 4-byte version + 4-byte body_size  (see protocol.Frame)
    Frame body:   JSON object  {"type": <int EventType>, "data": <any>}

Versioning:
    PROTOCOL_VERSION stamps every outgoing frame. Stream readers reject an
    unsupported header before reading its body; malformed Event bodies raise
    protocol errors. Version 1 carries engine_revision in peer identities. Standalone
    output sessions may use null. Engine-owned modules require a SHA-256 revision.

The wire contract stays free of framework serializers; the outbound payload
adapter is imported lazily only while encoding rich domain values.
"""

import json

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.protocol import Frame

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = frozenset({PROTOCOL_VERSION})


class UnsupportedProtocolVersion(ValueError):
    """Raised by the stream reader before consuming an unsupported frame body."""


class InvalidEventBody(ValueError):
    """A complete frame body is not a valid Event envelope."""


def require_supported_version(frame_version: int) -> None:
    """Validate a frame version before its peer-controlled body is read.

    Continuing after an unsupported header would require trusting/skipping its
    advertised body length, so the safe response is to close the connection.
    """
    if type(frame_version) is not int or frame_version not in SUPPORTED_VERSIONS:
        raise UnsupportedProtocolVersion(
            f"unsupported IPC protocol version {frame_version}"
        )


def event_to_body(event: Event) -> str:
    """
    Serialise an Event to the JSON body string (no framing).

    Rich domain payloads (e.g. TrialOutcome / Classification) are flattened to a
    JSON-safe form via the unified outbound contract, so pushing a TrialCompleted
    over the socket no longer fails on a non-serialisable VO.
    """
    from omnibci.infrastructure.outbound import payload_to_jsonable
    return json.dumps({"type": int(event.type), "data": payload_to_jsonable(event.data)})


def encode_event(event: Event) -> bytes:
    """Serialise an Event to full frame bytes, stamped with PROTOCOL_VERSION."""
    return Frame(event_to_body(event), version=PROTOCOL_VERSION).to_bytes()


def decode_event_body(body: bytes, frame_version: int) -> Event:
    """Strictly decode one complete Event body.

    Stream transports use this API after reading a bounded body.  Any malformed
    JSON, invalid envelope, or unknown event type is a protocol violation: frame
    alignment is known, but keeping the peer connected would let invalid traffic
    occupy a scarce connection indefinitely.
    """
    require_supported_version(frame_version)
    try:
        payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidEventBody(f"undecodable Event body: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidEventBody(
            f"Event body must be a JSON object, got {type(payload).__name__}"
        )
    raw_type = payload.get("type")
    if isinstance(raw_type, bool) or not isinstance(raw_type, int):
        raise InvalidEventBody(f"Event type must be an integer, got {raw_type!r}")
    try:
        event_type = EventType(raw_type)
    except ValueError as exc:
        raise InvalidEventBody(f"unknown Event type {raw_type!r}") from exc
    return Event(event_type=event_type, data=payload.get("data"))
