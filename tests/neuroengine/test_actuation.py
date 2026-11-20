"""
Tests for Phase 3: the actuation bounded context (engine listens; controller
dials in, like stimulation).

Covers:
  - ActuationCommandTranslator: TrialCompleted (TrialOutcome — the real bus
    payload emitted by SignalProcessor) → ActuationCommand on the bus;
    payload carries target_id/event_id/confidence; bare Classification is
    rejected; noncanonical trials emit nothing.
  - ActuationFeedbackHandler: ack/state/fault accounting.
  - actuation_peer_config: ListenTransport + correct route white-lists.
  - Builder: actuation wiring is opt-in (off by default; on when enabled).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.application.events import TrialOutcome
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.neuroengine.actuation_feedback import ActuationFeedbackHandler
from omnibci.infrastructure.neuroengine.gateway.peers import actuation_peer_config, ACTUATION
from omnibci.infrastructure.ipc.peer_transport import ListenTransport


class _Bus:
    def __init__(self):
        self.put = []

    def put_event(self, event):
        self.put.append(event)


class TestActuationCommandTranslator:
    def test_trial_outcome_payload_emits_command(self):
        """Real bus shape: TrialOutcome wrapping the Classification (B1 regression)."""
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        outcome = TrialOutcome(
            generation="run-1",
            trial_number=3,
            classification=Classification(target_id=5, event_id=2, confidence=0.9),
        )
        tr.on_trial_completed(Event(EventType.TrialCompleted, outcome))
        assert len(bus.put) == 1
        cmd = bus.put[0]
        assert cmd.type == EventType.ActuationCommand
        assert cmd.data.parameters["target_id"] == 5
        assert cmd.data.parameters["event_id"] == 2
        assert cmd.data.parameters["confidence"] == 0.9

    def test_bare_classification_payload_is_rejected(self):
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, Classification(5, 2, 0.9)))
        assert bus.put == []

    def test_payload_without_target_id_emits_nothing(self):
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))
        assert bus.put == []

    def test_commands_carry_unique_identity_and_issued_at(self):
        import time
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        before = time.time()
        for tid in (1, 2, 3):
            tr.on_trial_completed(Event(EventType.TrialCompleted,
                TrialOutcome("run", tid, Classification(tid, 1, 0.9))))
        after = time.time()
        assert len({event.data.command_id for event in bus.put}) == 3
        for event in bus.put:
            assert before <= event.data.issued_at <= after

    def test_skipped_payload_does_not_emit_an_intent(self):
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))
        tr.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome("run", 1, Classification(1, 1))))
        assert len(bus.put) == 1
        assert bus.put[0].data.origin["trial_number"] == 1


class TestActuationFeedbackHandler:
    def test_ack_state_fault_accounting(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "1"}))
        h.on_feedback(Event(EventType.ActuationState, {"pos": 1.0}))
        h.on_feedback(Event(EventType.ActuationFault, {"code": "ESTOP"}))
        assert h.ack_count == 1
        assert h.fault_count == 1
        assert h.last_state == {"pos": 1.0}
        assert h.last_fault == {"code": "ESTOP"}

    def test_acks_bucketed_by_status(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "1"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "superseded", "command_id": "2"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "stale", "command_id": "3"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "4"}))
        assert h.ack_count == 4                      # total preserved
        assert (h.ok_count, h.superseded_count, h.stale_count) == (2, 1, 1)
        assert h.last_acked_command_id == "4"

    def test_ack_without_status_and_identity_is_rejected(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"ok": True}))
        assert h.ack_count == 0 and h.last_acked_command_id is None

    @pytest.mark.parametrize("status", [[], {}, None, True])
    def test_malformed_ack_status_is_ignored(self, status):
        handler = ActuationFeedbackHandler()
        handler.on_feedback(Event(EventType.ActuationAck, {"status": status, "command_id": "1"}))
        assert handler.ack_count == 0

    def test_last_command_identity_is_retained(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "1"}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "command_id": "5"}))
        assert h.last_acked_command_id == "5"


class TestActuationPeerConfig:
    def test_routes_and_transport(self):
        cfg = actuation_peer_config(host="127.0.0.1", port=8001)
        assert cfg.name == ACTUATION
        assert isinstance(cfg.transport, ListenTransport)
        assert cfg.transport._server.max_clients == 1
        assert cfg.routes.outbound == frozenset({EventType.ActuationCommand, EventType.SettleActuation,
                                                EventType.ResumeActuation})
        assert cfg.routes.inbound == frozenset({
            EventType.ActuationAck, EventType.ActuationState, EventType.ActuationFault,
            EventType.CommandResult,
        })


class TestBuilderActuationFoundation:
    def _build(self, **cfg_kwargs):
        from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker", return_value=MagicMock()),
        ):
            return NeuroEngineBuilder(NeuroEngineConfig(**cfg_kwargs), settings_snapshot={}).build()

    def test_transport_is_deferred_and_commands_disabled_by_default(self):
        eng = self._build()
        assert isinstance(eng.actuation_translator, ActuationCommandTranslator)
        assert isinstance(eng.actuation_feedback, ActuationFeedbackHandler)
        assert ACTUATION not in eng.peer_gateway._channels
        assert not eng._actuation_active

    def test_output_parameters_do_not_configure_foundation_listeners(self):
        from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
        eng = NeuroEngineBuilder(settings_snapshot={"ACTUATION.server_port": "invalid-until-prepared"}).build()
        assert not eng.peer_gateway._channels
