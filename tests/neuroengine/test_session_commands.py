"""
Session-id push-down into the worker (docs/session-id-unification.md).

Covers:
  - SignalProcessor._execute: legacy bare-enum commands still work; tuple
    (command, payload) protocol drives start_session / end_session.
  - start_session enforces the single-active-session invariant (ends the
    previous session, fresh aggregate uses the API-supplied id).
  - end_session ignores a stale id (mismatch guard).
  - NeuroEngine._make_command_router: carry_data routes enqueue (command, data).
  - SignalProcessor standby mode (auto_start_session=False): no running
    session until the first start_session command.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from omnibci.domain.session import SessionState
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.neuroengine.processing.signal_processor import (
    SignalProcessor,
    SignalProcessorCommand,
)
from omnibci.shared.events import Event, EventType


def _processor(auto_start: bool = True) -> SignalProcessor:
    return SignalProcessor(
        detector=MagicMock(),
        decoder=MagicMock(),
        command_queue=MagicMock(),
        data_manager=MagicMock(),
        event_bus=MagicMock(),
        auto_start_session=auto_start,
    )


class TestCommandProtocol:
    def test_bare_should_stop_still_works(self):
        p = _processor()
        p._execute(SignalProcessorCommand.should_stop)
        assert p._should_stop is True

    def test_start_session_uses_pushed_id(self):
        p = _processor(auto_start=False)
        p._execute((SignalProcessorCommand.start_session,
                    {"session_id": "rest-id-1", "paradigm": "classic40SSVEP"}))
        assert p.session.session_id == "rest-id-1"
        assert p.session.paradigm == "classic40SSVEP"
        assert p.session.state == SessionState.RUNNING

    def test_start_session_bad_payload_ignored(self):
        p = _processor(auto_start=False)
        before = p.session
        p._execute((SignalProcessorCommand.start_session, None))
        p._execute((SignalProcessorCommand.start_session, {"paradigm": "x"}))
        assert p.session is before  # unchanged

    def test_second_start_ends_previous_session(self):
        p = _processor(auto_start=False)
        p._execute((SignalProcessorCommand.start_session, {"session_id": "s1"}))
        first = p.session
        p._execute((SignalProcessorCommand.start_session, {"session_id": "s2"}))
        assert first.state == SessionState.ENDED
        assert p.session.session_id == "s2"
        assert p.session.state == SessionState.RUNNING

    def test_end_session_matching_id_ends(self):
        p = _processor(auto_start=False)
        p._execute((SignalProcessorCommand.start_session, {"session_id": "s1"}))
        p._execute((SignalProcessorCommand.end_session, "s1"))
        assert p.session.state == SessionState.ENDED

    def test_end_session_stale_id_ignored(self):
        p = _processor(auto_start=False)
        p._execute((SignalProcessorCommand.start_session, {"session_id": "s2"}))
        p._execute((SignalProcessorCommand.end_session, "s1"))  # stale
        assert p.session.state == SessionState.RUNNING


class TestStandbyMode:
    def test_auto_start_false_means_idle_session(self):
        p = _processor(auto_start=False)
        assert p.session.state == SessionState.IDLE

    def test_auto_start_true_keeps_legacy_behaviour(self):
        p = _processor(auto_start=True)
        assert p.session.state == SessionState.RUNNING


class _FakeQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


class TestCarryDataRouting:
    def test_plain_route_enqueues_bare_command(self):
        q = _FakeQueue()
        route = NeuroEngine._make_command_router(q, AcquisitionCommand.stop_stream)
        route(Event(EventType.StopStream))
        assert q.items == [AcquisitionCommand.stop_stream]

    def test_carry_data_route_enqueues_tuple(self):
        q = _FakeQueue()
        route = NeuroEngine._make_command_router(
            q, SignalProcessorCommand.start_session, carry_data=True)
        payload = {"session_id": "abc", "paradigm": None}
        route(Event(EventType.StartSession, payload))
        assert q.items == [(SignalProcessorCommand.start_session, payload)]
