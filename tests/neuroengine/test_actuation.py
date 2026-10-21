"""
Tests for Phase 3: the actuation bounded context (engine listens; controller
dials in, like stimulation).

Covers:
  - ActuationCommandTranslator: TrialCompleted (TrialOutcome — the real bus
    payload emitted by SignalProcessor) → ActuationCommand on the bus;
    payload carries target_id/event_id/confidence; bare Classification is
    accepted for back-compat; payloads without target_id emit nothing.
  - ActuationFeedbackHandler: ack/state/fault accounting.
  - actuation_peer_config: ListenTransport + correct route white-lists.
  - Builder: actuation wiring is opt-in (off by default; on when enabled).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.infrastructure.outbound import TrialOutcome
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
        assert cmd.data["target_id"] == 5
        assert cmd.data["event_id"] == 2
        assert cmd.data["confidence"] == 0.9

    def test_bare_classification_payload_still_accepted(self):
        """Back-compat: pre-enrichment bare Classification payload."""
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        clf = Classification(target_id=5, event_id=2, confidence=0.9)
        tr.on_trial_completed(Event(EventType.TrialCompleted, clf))
        assert len(bus.put) == 1
        cmd = bus.put[0]
        assert cmd.type == EventType.ActuationCommand
        assert cmd.data["target_id"] == 5
        assert cmd.data["event_id"] == 2
        assert cmd.data["confidence"] == 0.9

    def test_payload_without_target_id_emits_nothing(self):
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))
        assert bus.put == []

    def test_commands_carry_monotonic_seq_and_issued_at(self):
        import time
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        before = time.time()
        for tid in (1, 2, 3):
            tr.on_trial_completed(Event(
                EventType.TrialCompleted,
                Classification(target_id=tid, event_id=1, confidence=0.9)))
        after = time.time()
        assert [c.data["seq"] for c in bus.put] == [1, 2, 3]
        for c in bus.put:
            assert before <= c.data["issued_at"] <= after

    def test_skipped_payload_does_not_consume_seq(self):
        bus = _Bus()
        tr = ActuationCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))   # no target_id
        tr.on_trial_completed(Event(
            EventType.TrialCompleted,
            Classification(target_id=1, event_id=1)))
        assert bus.put[0].data["seq"] == 1


class TestActuationFeedbackHandler:
    def test_ack_state_fault_accounting(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"ok": True}))
        h.on_feedback(Event(EventType.ActuationState, {"pos": 1.0}))
        h.on_feedback(Event(EventType.ActuationFault, {"code": "ESTOP"}))
        assert h.ack_count == 1
        assert h.fault_count == 1
        assert h.last_state == {"pos": 1.0}
        assert h.last_fault == {"code": "ESTOP"}

    def test_acks_bucketed_by_status(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "seq": 1}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "superseded", "seq": 2}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "stale", "seq": 3}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "seq": 4}))
        assert h.ack_count == 4                      # total preserved
        assert (h.ok_count, h.superseded_count, h.stale_count) == (2, 1, 1)
        assert h.last_acked_seq == 4

    def test_ack_without_status_counts_as_ok(self):
        # Back-compat: a bare dict ack (older sender) defaults to ok.
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"ok": True}))
        assert h.ok_count == 1 and h.last_acked_seq is None

    def test_seq_gap_is_tolerated(self):
        h = ActuationFeedbackHandler()
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "seq": 1}))
        h.on_feedback(Event(EventType.ActuationAck, {"status": "ok", "seq": 5}))
        assert h.last_acked_seq == 5                 # gap logged, not raised


class TestActuationPeerConfig:
    def test_routes_and_transport(self):
        cfg = actuation_peer_config(host="127.0.0.1", port=8001)
        assert cfg.name == ACTUATION
        assert isinstance(cfg.transport, ListenTransport)
        assert cfg.transport._server.max_clients == 1
        assert cfg.routes.outbound == frozenset({EventType.ActuationCommand, EventType.SettleActuation})
        assert cfg.routes.inbound == frozenset({
            EventType.ActuationAck, EventType.ActuationState, EventType.ActuationFault,
            EventType.CommandResult,
        })


class TestBuilderActuationOptIn:
    def _build(self, **cfg_kwargs):
        from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker", return_value=MagicMock()),
        ):
            return NeuroEngineBuilder(NeuroEngineConfig(**cfg_kwargs), settings_snapshot={}).build()

    def test_disabled_by_default(self):
        eng = self._build(actuation_enabled=False)
        assert eng.actuation_translator is None
        assert eng.actuation_feedback is None
        # only the stimulation peer is registered
        assert ACTUATION not in eng.peer_gateway._channels

    def test_enabled_wires_channel_and_components(self):
        eng = self._build(actuation_enabled=True, actuation_server_port=8042)
        assert isinstance(eng.actuation_translator, ActuationCommandTranslator)
        assert isinstance(eng.actuation_feedback, ActuationFeedbackHandler)
        ch = eng.peer_gateway.peer(ACTUATION)
        assert ch._config.transport._server.port == 8042
