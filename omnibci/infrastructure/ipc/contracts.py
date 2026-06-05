"""
Stimulation IPC message contract.

This is the single source of truth for the *wire shape* and *protocol version*
of the engine ↔ stimulation socket channel.  Both directions (NetworkServer and
NetworkClient) encode/decode through here, so the format can never drift between
ends — the class of bug that previously made the channel one-directional
(``event_type`` vs ``type``, ``to_dict`` vs ``to_json``).

Wire format (backward compatible with the previous ad-hoc JSON):
    Frame header: 4-byte version + 4-byte body_size  (see protocol.Frame)
    Frame body:   JSON object  {"type": <int EventType>, "data": <any>}

Versioning:
    PROTOCOL_VERSION stamps every outgoing frame.  On receive, the frame's
    header version is validated against SUPPORTED_VERSIONS; an unsupported
    version is dropped (logged) rather than mis-parsed.  Bump PROTOCOL_VERSION
    and extend SUPPORTED_VERSIONS when the body shape changes incompatibly.

Kept dependency-free (stdlib + shared kernel only): the IPC layer must not pull
in pydantic or any heavier framework.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.protocol import Frame

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})


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


def body_to_event(body: bytes, frame_version: int) -> Optional[Event]:
    """
    Decode a frame body into an Event, validating the protocol version.

    Returns None (and logs) for an unsupported version, malformed JSON, or an
    unknown event type — so one bad frame never tears down the receive loop.
    """
    if frame_version not in SUPPORTED_VERSIONS:
        logger.warning("IPC: unsupported protocol version %s; frame dropped", frame_version)
        return None
    try:
        payload = json.loads(body.decode())
    except (ValueError, UnicodeDecodeError) as exc:
        logger.warning("IPC: undecodable frame body dropped: %s", exc)
        return None
    raw_type = payload.get("type")
    try:
        event_type = EventType(raw_type)
    except (ValueError, TypeError):
        logger.warning("IPC: unknown event type %r dropped", raw_type)
        return None
    return Event(event_type=event_type, data=payload.get("data"))
