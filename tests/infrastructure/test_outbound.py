"""
Tests for the unified outbound event contract (infrastructure/outbound.py) and
its use by the ACL — fixes the WS messages that hardcoded session_id/trial_number
and the socket push that failed to serialise a Classification VO.

All pure (domain + shared + numpy) — sandbox-runnable, no FastAPI/settings.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.domain.events import (
    DomainEvent,
    SessionEnded as DomainSessionEnded,
    SessionStarted as DomainSessionStarted,
    TrialCompleted as DomainTrialCompleted,
)
from omnibci.infrastructure.outbound import (
    TrialOutcome,
    SessionSummary,
    event_to_wire,
    payload_to_jsonable,
    make_stimulation_ack,
)
from omnibci.application.events import (
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)
from omnibci.infrastructure.event_translator import DomainEventPublisher


_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _clf(target_id=3, event_id=101, confidence=0.85):
    return Classification(target_id=target_id, event_id=event_id,
                          confidence=confidence, produced_at=_TS)


# ---------------------------------------------------------------------------
# event_to_wire
# ---------------------------------------------------------------------------

class TestEventToWire:
    def test_trial_completed_carries_session_context(self):
        outcome = TrialOutcome(session_id="sess-1", trial_number=4, classification=_clf(target_id=7))
        wire = event_to_wire(Event(EventType.TrialCompleted, outcome))
        assert wire == {
            "event": "TrialCompleted",
            "session_id": "sess-1",
            "trial_number": 4,
            "target_id": 7,
            "event_id": 101,
            "confidence": 0.85,
            "forced": False,
            "produced_at": _TS.isoformat(),
        }

    def test_trial_completed_bare_classification_rejected(self):
        assert event_to_wire(Event(EventType.TrialCompleted, _clf())) is None

    def test_session_started(self):
        wire = event_to_wire(Event(EventType.SessionStarted, "sess-2"))
        assert wire == {"event": "SessionStarted", "session_id": "sess-2"}

    def test_session_ended_with_summary_carries_session_id(self):
        wire = event_to_wire(Event(EventType.SessionEnded, SessionSummary("sess-3", 5)))
        assert wire == {"event": "SessionEnded", "session_id": "sess-3", "total_trials": 5}

    def test_session_ended_bare_int_rejected(self):
        assert event_to_wire(Event(EventType.SessionEnded, 5)) is None

    def test_stimulation_ack_from_dict(self):
        data = make_stimulation_ack("StartStimTrial", status="ok", target_index=3)
        wire = event_to_wire(Event(EventType.StimulationAck, data))
        assert wire == {
            "event": "StimulationAck", "command": "StartStimTrial",
            "target_index": 3, "status": "ok", "message": None,
        }

    def test_stimulation_ack_error(self):
        data = make_stimulation_ack("StartStimTrial", status="error", message="renderer busy")
        wire = event_to_wire(Event(EventType.StimulationAck, data))
        assert wire["status"] == "error" and wire["message"] == "renderer busy"

    def test_unmapped_event_returns_none(self):
        assert event_to_wire(Event(EventType.StartStream, None)) is None

    def test_wire_is_json_serialisable(self):
        outcome = TrialOutcome("s", 1, _clf())
        json.dumps(event_to_wire(Event(EventType.TrialCompleted, outcome)))  # must not raise


# ---------------------------------------------------------------------------
# payload_to_jsonable (socket envelope data)
# ---------------------------------------------------------------------------

class TestPayloadToJsonable:
    def test_command_request_is_flattened_recursively(self):
        out = payload_to_jsonable(CommandRequest(
            command_id="cmd-1",
            payload={"n_trials": 1, "trial_params": [7]},
        ))
        assert out == {
            "command_id": "cmd-1",
            "payload": {"n_trials": 1, "trial_params": [7]},
        }
        json.dumps(out)

    def test_command_result_is_flattened(self):
        out = payload_to_jsonable(CommandResult(
            command_id="cmd-1",
            command="StartStimTrial",
            ok=False,
            message="renderer busy",
            error_code="STIMULATION_PRESENT_FAILED",
            failure_kind=CommandFailureKind.EXECUTION_FAILED,
        ))
        assert out["command_id"] == "cmd-1"
        assert out["ok"] is False
        assert out["failure_kind"] == "execution_failed"
        json.dumps(out)

    def test_trial_outcome_flattened_and_serialisable(self):
        out = payload_to_jsonable(TrialOutcome("s", 2, _clf(target_id=9)))
        assert out["session_id"] == "s" and out["trial_number"] == 2 and out["target_id"] == 9
        json.dumps(out)  # must not raise

    def test_bare_classification_passthrough(self):
        # Unknown values pass through so the socket encoder exposes the issue.
        clf = _clf(target_id=4)
        assert payload_to_jsonable(clf) is clf

    def test_scalar_passthrough(self):
        assert payload_to_jsonable(7) == 7
        assert payload_to_jsonable("x") == "x"


# ---------------------------------------------------------------------------
# ACL now emits TrialOutcome with session context
# ---------------------------------------------------------------------------

class _SpyBus:
    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


class TestAclEmitsTrialOutcome:
    def test_trial_completed_payload_is_trial_outcome(self):
        bus = _SpyBus()
        pub = DomainEventPublisher(bus)
        domain_event = DomainTrialCompleted(
            session_id="sess-9", trial_number=2, classification=_clf(event_id=101),
        )
        pub.publish(domain_event)
        assert len(bus.events) == 1
        payload = bus.events[0].data
        assert isinstance(payload, TrialOutcome)
        assert payload.session_id == "sess-9"
        assert payload.trial_number == 2
        # and it flows through to a full WS wire dict with real context
        wire = event_to_wire(bus.events[0])
        assert wire["session_id"] == "sess-9" and wire["trial_number"] == 2

    def test_session_started_maps_to_session_id_payload(self):
        bus = _SpyBus()
        DomainEventPublisher(bus).publish(
            DomainSessionStarted("sid-1", paradigm="p")
        )
        assert bus.events[0].type == EventType.SessionStarted
        assert bus.events[0].data == "sid-1"

    def test_session_ended_maps_to_session_summary(self):
        bus = _SpyBus()
        DomainEventPublisher(bus).publish(
            DomainSessionEnded("sess-9", total_trials=3)
        )
        assert bus.events[0].type == EventType.SessionEnded
        payload = bus.events[0].data
        assert isinstance(payload, SessionSummary)
        assert payload.session_id == "sess-9"
        assert payload.total_trials == 3

    def test_publish_all_preserves_event_order(self):
        bus = _SpyBus()
        DomainEventPublisher(bus).publish_all([
            DomainSessionStarted("sess-9", paradigm="p"),
            DomainTrialCompleted("sess-9", 1, _clf()),
            DomainSessionEnded("sess-9", total_trials=1),
        ])
        assert [event.type for event in bus.events] == [
            EventType.SessionStarted,
            EventType.TrialCompleted,
            EventType.SessionEnded,
        ]

    def test_unknown_event_is_ignored(self):
        bus = _SpyBus()
        DomainEventPublisher(bus).publish(DomainEvent())
        assert bus.events == []
