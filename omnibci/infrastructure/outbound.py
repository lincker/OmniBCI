"""Map EventBus events to the shared JSON-safe outbound wire contract."""

import logging
from typing import Any, Optional

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
# Re-export payloads to preserve the established import path.
from omnibci.application.events import (
    CommandRequest,
    CommandResult,
    SessionSummary,
    TrialOutcome,
)

__all__ = ["TrialOutcome", "SessionSummary", "event_to_wire",
           "make_stimulation_ack", "payload_to_jsonable"]

logger = logging.getLogger(__name__)


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
        if not isinstance(data, TrialOutcome):
            logger.error("event_to_wire: TrialCompleted payload is %s, not TrialOutcome",
                         type(data).__name__)
            return None
        return {"event": "TrialCompleted", "session_id": data.session_id,
                "trial_number": data.trial_number,
                **({"config_revision": data.config_revision} if data.config_revision is not None else {}),
                **_classification_fields(data.classification)}

    if etype == EventType.SessionStarted:
        return {"event": "SessionStarted", "session_id": str(data)}

    if etype == EventType.SessionEnded:
        if not isinstance(data, SessionSummary):
            logger.error("event_to_wire: SessionEnded payload is %s, not SessionSummary",
                         type(data).__name__)
            return None
        return {"event": "SessionEnded", "session_id": data.session_id,
                "total_trials": data.total_trials}

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


def make_actuation_ack(target_id: Optional[int] = None,
                       status: str = "ok",
                       message: Optional[str] = None,
                       seq: Optional[int] = None) -> dict:
    """
    Build the JSON-safe payload the actuation process sends back to ack an
    ActuationCommand. Mirrors make_stimulation_ack. *seq* echoes the
    command's correlation id (statuses: ok | superseded | stale).
    """
    return {"target_id": target_id, "status": status, "message": message,
            "seq": seq}


def make_actuation_fault(code: str,
                         target_id: Optional[int] = None,
                         message: Optional[str] = None,
                         seq: Optional[int] = None) -> dict:
    """
    Build the JSON-safe ActuationFault payload (same {"code", ...} shape as
    the engine watchdog's ACTUATION_LINK_LOST). *seq* echoes the failing
    command's correlation id when there is one.
    """
    return {"code": code, "source": "actuation_subsystem",
            "target_id": target_id, "message": message, "seq": seq}


def payload_to_jsonable(data: Any) -> Any:
    """
    Convert an EventBus payload into a JSON-serialisable value for the socket
    envelope (``{"type", "data"}``).  Rich domain payloads are flattened; plain
    scalars pass through unchanged.
    """
    if isinstance(data, TrialOutcome):
        return {"session_id": data.session_id, "trial_number": data.trial_number,
                **({"config_revision": data.config_revision} if data.config_revision is not None else {}),
                **_classification_fields(data.classification)}
    if isinstance(data, SessionSummary):
        return {"session_id": data.session_id, "total_trials": data.total_trials}
    if isinstance(data, CommandRequest):
        return {
            "command_id": data.command_id,
            "payload": payload_to_jsonable(data.payload),
        }
    if isinstance(data, CommandResult):
        return {
            "command_id": data.command_id,
            "command": data.command,
            "ok": data.ok,
            "message": data.message,
            "error_code": data.error_code,
            "failure_kind": data.failure_kind,
            **({"data": payload_to_jsonable(data.data)} if data.data is not None else {}),
        }
    return data
