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
from typing import Any, Optional

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
# P2-8：载荷类型迁至 application 层（application 服务需对其做显式类型
# 检查）；此处 re-export 以保持既有导入路径可用。
from omnibci.application.events import SessionSummary, TrialOutcome

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
        # P2-8：TrialCompleted 载荷从此只有 TrialOutcome 一种形状；
        # bare-Classification 兼容分支已删（发布端全部走 DomainEventPublisher）。
        if not isinstance(data, TrialOutcome):
            logger.error("event_to_wire: TrialCompleted payload is %s, not TrialOutcome",
                         type(data).__name__)
            return None
        return {"event": "TrialCompleted", "session_id": data.session_id,
                "trial_number": data.trial_number,
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
                **_classification_fields(data.classification)}
    if isinstance(data, SessionSummary):
        return {"session_id": data.session_id, "total_trials": data.total_trials}
    return data
