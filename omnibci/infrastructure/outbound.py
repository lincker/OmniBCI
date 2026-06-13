"""
Unified outbound event contract.

Both outbound transports — the WebSocket broadcaster (presentation) and the
stimulation socket (infrastructure/ipc) — carry the *same* domain events
(TrialCompleted / SessionStarted / SessionEnded).  Before this, each built its
own wire representation: the WS broadcaster constructed pydantic messages with
hardcoded ``session_id="current"`` / ``trial_number=0`` (the EventBus payload
had dropped that context), and the socket tried to ``json.dumps`` a raw
Classification VO (not JSON-serialisable) — so both were subtly broken.

This module is the single place that maps an infrastructure ``Event`` to a
JSON-safe outbound dict.  Both transports import it, so the shape can never
drift between them.

Layer note: lives in ``infrastructure/`` (not ``shared/``) because it references
the domain ``Classification`` VO; ``shared/`` must stay dependency-free.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrialOutcome:
    """
    Rich EventBus payload for TrialCompleted — carries the session context the
    bare Classification lacked.  Picklable (crosses the mp.Queue): str + int +
    frozen Classification dataclass.
    """
    session_id: str
    trial_number: int
    classification: Classification


@dataclass(frozen=True)
class SessionSummary:
    """
    Rich EventBus payload for SessionEnded — carries session_id alongside the
    trial count (the bare int payload dropped session_id).  Picklable.
    """
    session_id: str
    total_trials: int


def _classification_fields(clf: Classification) -> dict:
    return {
        "target_id": clf.target_id,
        "event_id": clf.event_id,
        "confidence": clf.confidence,
        "forced": clf.forced,
        "produced_at": clf.produced_at.isoformat(),
    }


def event_to_wire(event: Event) -> Optional[dict]:
    """
    Map an infrastructure Event to a JSON-safe outbound message dict.

    The top-level ``event`` key is the discriminator clients route on (matches
    ``presentation/schemas/ws.py``).  Returns None for events with no outbound
    representation.
    """
    etype = event.type
    data = event.data

    if etype == EventType.TrialCompleted:
        if isinstance(data, TrialOutcome):
            session_id, trial_number, clf = data.session_id, data.trial_number, data.classification
        elif isinstance(data, Classification):
            # Back-compat: a bare Classification payload (pre-enrichment).
            session_id, trial_number, clf = None, 0, data
        else:
            logger.error("event_to_wire: TrialCompleted payload is %s, not "
                         "TrialOutcome/Classification", type(data).__name__)
            return None
        return {"event": "TrialCompleted", "session_id": session_id,
                "trial_number": trial_number, **_classification_fields(clf)}

    if etype == EventType.SessionStarted:
        return {"event": "SessionStarted", "session_id": str(data)}

    if etype == EventType.SessionEnded:
        if isinstance(data, SessionSummary):
            return {"event": "SessionEnded", "session_id": data.session_id,
                    "total_trials": data.total_trials}
        # Back-compat: bare total_trials int (pre-enrichment), no session_id.
        return {"event": "SessionEnded", "session_id": None, "total_trials": int(data)}

    if etype == EventType.StimulationAck:
        # Payload arrives from the stimulation process as a JSON dict over the
        # socket (see make_stimulation_ack); tolerate missing keys.
        d = data if isinstance(data, dict) else {}
        return {
            "event": "StimulationAck",
            "command": str(d.get("command", "")),
            "target_index": d.get("target_index"),
            "status": d.get("status", "ok"),
            "message": d.get("message"),
        }

    return None


def make_stimulation_ack(command: str, status: str = "ok",
                         target_index: Optional[int] = None,
                         message: Optional[str] = None) -> dict:
    """
    Build the JSON-safe payload the stimulation process sends back to ack a
    command.  Used on the stimulation side as::

        client.send_event(Event(EventType.StimulationAck,
                                 make_stimulation_ack("StartStimTrial",
                                                      target_index=3)))
    """
    return {"command": command, "status": status,
            "target_index": target_index, "message": message}


def payload_to_jsonable(data: Any) -> Any:
    """
    Convert an EventBus payload into a JSON-serialisable value for the socket
    envelope (``{"type", "data"}``).  Rich domain payloads are flattened; plain
    scalars pass through unchanged.
    """
    if isinstance(data, TrialOutcome):
        return {"session_id": data.session_id, "trial_number": data.trial_number,
                **_classification_fields(data.classification)}
    if isinstance(data, SessionSummary):
        return {"session_id": data.session_id, "total_trials": data.total_trials}
    if isinstance(data, Classification):
        return _classification_fields(data)
    return data
