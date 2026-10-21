"""Decoded outcomes use one JSON-safe contract for WebSocket and peer clients."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.infrastructure.outbound import (
    TrialOutcome,
    event_to_wire,
    payload_to_jsonable,
    make_stimulation_ack,
)
from omnibci.application.events import (
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)


_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _clf(target_id=3, event_id=101, confidence=0.85):
    return Classification(target_id=target_id, event_id=event_id,
                          confidence=confidence, produced_at=_TS)


# ---------------------------------------------------------------------------
# event_to_wire
# ---------------------------------------------------------------------------

class TestEventToWire:
    def test_trial_completed_carries_runtime_context(self):
        outcome = TrialOutcome(generation="run-1", trial_number=4, classification=_clf(target_id=7))
        wire = event_to_wire(Event(EventType.TrialCompleted, outcome))
        assert wire == {
            "event": "TrialCompleted",
            "generation": "run-1",
            "trial_number": 4,
            "target_id": 7,
            "event_id": 101,
            "confidence": 0.85,
            "forced": False,
            "produced_at": _TS.isoformat(),
        }

    def test_trial_completed_bare_classification_rejected(self):
        assert event_to_wire(Event(EventType.TrialCompleted, _clf())) is None

    def test_trial_completed_retains_the_epoch_configuration_revision(self):
        outcome = TrialOutcome("run-1", 1, _clf(), config_revision="epoch-revision")
        wire = event_to_wire(Event(EventType.TrialCompleted, outcome))
        assert wire["config_revision"] == "epoch-revision"
        assert payload_to_jsonable(outcome) == {key: value for key, value in wire.items() if key != "event"}

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
        assert out["generation"] == "s" and out["trial_number"] == 2 and out["target_id"] == 9
        json.dumps(out)  # must not raise

    def test_bare_classification_passthrough(self):
        # Unknown values pass through so the socket encoder exposes the issue.
        clf = _clf(target_id=4)
        assert payload_to_jsonable(clf) is clf

    def test_scalar_passthrough(self):
        assert payload_to_jsonable(7) == 7
        assert payload_to_jsonable("x") == "x"
