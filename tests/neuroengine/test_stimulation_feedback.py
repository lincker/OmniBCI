"""
Tests for Phase 2: the engine consumes stimulation → engine inbound feedback.

Covers:
  - StimulationFeedbackHandler: ok/error counting, last_ack, tolerant parsing.
  - NeuroEngine registers the handler on EventType.StimulationAck.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.neuroengine.stimulation_feedback import StimulationFeedbackHandler


def _ack(command="StartStimTrial", status="ok", target_index=3, message=None) -> Event:
    return Event(EventType.StimulationAck,
                 {"command": command, "status": status,
                  "target_index": target_index, "message": message})


class TestStimulationFeedbackHandler:
    def test_ok_ack_counted(self):
        h = StimulationFeedbackHandler()
        h.on_ack(_ack(status="ok"))
        assert h.ok_count == 1 and h.error_count == 0
        assert h.last_ack["target_index"] == 3

    def test_error_ack_counted(self):
        h = StimulationFeedbackHandler()
        h.on_ack(_ack(status="error", message="renderer crashed"))
        assert h.error_count == 1 and h.ok_count == 0
        assert h.last_ack["message"] == "renderer crashed"

    def test_mixed_sequence(self):
        h = StimulationFeedbackHandler()
        for s in ("ok", "ok", "error", "ok"):
            h.on_ack(_ack(status=s))
        assert (h.ok_count, h.error_count) == (3, 1)

    def test_missing_status_defaults_ok(self):
        h = StimulationFeedbackHandler()
        h.on_ack(Event(EventType.StimulationAck, {"command": "X"}))
        assert h.ok_count == 1

    def test_non_dict_payload_tolerated(self):
        h = StimulationFeedbackHandler()
        h.on_ack(Event(EventType.StimulationAck, None))   # must not raise
        assert h.ok_count == 1
        assert h.last_ack == {}


class TestEngineRegistersStimulationAck:
    def _engine(self):
        from omnibci.infrastructure.neuroengine.engine import NeuroEngine
        return NeuroEngine(
            event_bus=MagicMock(),
            data_manager=MagicMock(),
            acquisition_worker=MagicMock(),
            acquisition_command_queue=MagicMock(),
            signal_processing_worker=MagicMock(),
            processor_command_queue=MagicMock(),
            peer_gateway=MagicMock(),
        )

    def test_stimulation_ack_listener_registered(self):
        eng = self._engine()
        eng._register_event_listeners()
        registered = {
            call.args[0] for call in eng.event_bus.register_event_listener.call_args_list
        }
        assert EventType.StimulationAck in registered

    def test_handler_instance_present(self):
        eng = self._engine()
        assert isinstance(eng.stim_feedback, StimulationFeedbackHandler)
