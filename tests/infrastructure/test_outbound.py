"""
Tests for the unified outbound event contract (infrastructure/outbound.py) and
its use by the ACL — fixes the WS messages that hardcoded session_id/trial_number
and the socket push that failed to serialise a Classification VO.

All pure (domain + shared + numpy) — sandbox-runnable, no FastAPI/settings.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import numpy as np

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.domain.events import TrialCompleted as DomainTrialCompleted
from omnibci.infrastructure.outbound import (
    TrialOutcome,
    SessionSummary,
    event_to_wire,
    payload_to_jsonable,
    make_stimulation_ack,
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
        # P2-8：bare-Classification legacy 形状已删除 → None（并记错误日志）
        assert event_to_wire(Event(EventType.TrialCompleted, _clf())) is None

    def test_session_started(self):
        wire = event_to_wire(Event(EventType.SessionStarted, "sess-2"))
        assert wire == {"event": "SessionStarted", "session_id": "sess-2"}

    def test_session_ended_with_summary_carries_session_id(self):
        wire = event_to_wire(Event(EventType.SessionEnded, SessionSummary("sess-3", 5)))
        assert wire == {"event": "SessionEnded", "session_id": "sess-3", "total_trials": 5}

    def test_session_ended_bare_int_rejected(self):
        # P2-8：bare-int legacy 形状已删除 → None（并记错误日志）
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
    def test_trial_outcome_flattened_and_serialisable(self):
        out = payload_to_jsonable(TrialOutcome("s", 2, _clf(target_id=9)))
        assert out["session_id"] == "s" and out["trial_number"] == 2 and out["target_id"] == 9
        json.dumps(out)  # must not raise

    def test_bare_classification_passthrough(self):
        # P2-8：Classification 不再是任何事件的独立载荷；payload_to_jsonable
        # 对未知类型原样透传（socket 编码方 json.dumps 时自然报错暴露问题）。
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
        epoch = Epoch(data=np.ones((2, 4)), event_id=101, sample_rate=1000, channel_count=2)
        domain_event = DomainTrialCompleted(
            session_id="sess-9", trial_number=2, epoch=epoch, classification=_clf(event_id=101),
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
