"""
Tests for Phase 3: the effector bounded context (engine listens; controller
dials in, like stimulation).

Covers:
  - EffectorCommandTranslator: TrialCompleted (TrialOutcome — the real bus
    payload emitted by DomainEventPublisher) → EffectorCommand on the bus;
    payload carries target_id/event_id/confidence; bare Classification is
    accepted for back-compat; payloads without target_id emit nothing.
  - EffectorFeedbackHandler: ack/state/fault accounting.
  - effector_peer_config: ListenTransport + correct route white-lists.
  - Builder: effector wiring is opt-in (off by default; on when enabled).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from omnibci.shared.events import Event, EventType
from omnibci.domain.classification import Classification
from omnibci.infrastructure.outbound import TrialOutcome
from omnibci.infrastructure.neuroengine.effector_translator import EffectorCommandTranslator
from omnibci.infrastructure.neuroengine.effector_feedback import EffectorFeedbackHandler
from omnibci.infrastructure.neuroengine.gateway.peers import effector_peer_config, EFFECTOR
from omnibci.infrastructure.ipc.peer_transport import ListenTransport


class _Bus:
    def __init__(self):
        self.put = []

    def put_event(self, event):
        self.put.append(event)


class TestEffectorCommandTranslator:
    def test_trial_outcome_payload_emits_command(self):
        """Real bus shape: TrialOutcome wrapping the Classification (B1 regression)."""
        bus = _Bus()
        tr = EffectorCommandTranslator(bus)
        outcome = TrialOutcome(
            session_id="sess-1",
            trial_number=3,
            classification=Classification(target_id=5, event_id=2, confidence=0.9),
        )
        tr.on_trial_completed(Event(EventType.TrialCompleted, outcome))
        assert len(bus.put) == 1
        cmd = bus.put[0]
        assert cmd.type == EventType.EffectorCommand
        assert cmd.data["target_id"] == 5
        assert cmd.data["event_id"] == 2
        assert cmd.data["confidence"] == 0.9

    def test_bare_classification_payload_still_accepted(self):
        """Back-compat: pre-enrichment bare Classification payload."""
        bus = _Bus()
        tr = EffectorCommandTranslator(bus)
        clf = Classification(target_id=5, event_id=2, confidence=0.9)
        tr.on_trial_completed(Event(EventType.TrialCompleted, clf))
        assert len(bus.put) == 1
        cmd = bus.put[0]
        assert cmd.type == EventType.EffectorCommand
        assert cmd.data["target_id"] == 5
        assert cmd.data["event_id"] == 2
        assert cmd.data["confidence"] == 0.9

    def test_payload_without_target_id_emits_nothing(self):
        bus = _Bus()
        tr = EffectorCommandTranslator(bus)
        tr.on_trial_completed(Event(EventType.TrialCompleted, object()))
        assert bus.put == []


class TestEffectorFeedbackHandler:
    def test_ack_state_fault_accounting(self):
        h = EffectorFeedbackHandler()
        h.on_feedback(Event(EventType.EffectorAck, {"ok": True}))
        h.on_feedback(Event(EventType.EffectorState, {"pos": 1.0}))
        h.on_feedback(Event(EventType.EffectorFault, {"code": "ESTOP"}))
        assert h.ack_count == 1
        assert h.fault_count == 1
        assert h.last_state == {"pos": 1.0}
        assert h.last_fault == {"code": "ESTOP"}


class TestEffectorPeerConfig:
    def test_routes_and_transport(self):
        cfg = effector_peer_config(host="127.0.0.1", port=8001)
        assert cfg.name == EFFECTOR
        assert isinstance(cfg.transport, ListenTransport)
        assert cfg.routes.outbound == frozenset({EventType.EffectorCommand})
        assert cfg.routes.inbound == frozenset({
            EventType.EffectorAck, EventType.EffectorState, EventType.EffectorFault,
        })


class TestBuilderEffectorOptIn:
    def _build(self, **cfg_kwargs):
        from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder, NeuroEngineConfig
        with (
            patch("omnibci.infrastructure.neuroengine.builder.AcquisitionWorker", return_value=MagicMock()),
            patch("omnibci.infrastructure.neuroengine.builder.SignalProcessingWorker", return_value=MagicMock()),
        ):
            return NeuroEngineBuilder(NeuroEngineConfig(**cfg_kwargs)).build()

    def test_disabled_by_default(self):
        eng = self._build(effector_enabled=False)
        assert eng.effector_translator is None
        assert eng.effector_feedback is None
        # only the stimulation peer is registered
        assert EFFECTOR not in eng.peer_gateway._channels

    def test_enabled_wires_channel_and_components(self):
        eng = self._build(effector_enabled=True, effector_server_port=8042)
        assert isinstance(eng.effector_translator, EffectorCommandTranslator)
        assert isinstance(eng.effector_feedback, EffectorFeedbackHandler)
        ch = eng.peer_gateway.peer(EFFECTOR)
        assert ch._config.transport._server.port == 8042
