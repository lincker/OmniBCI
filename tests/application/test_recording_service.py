"""
Unit tests for RecordingService — the session+stream orchestration.

Verifies the ordering rules and rollback without a real engine: a fake
SessionAppService records create/end calls and exposes active_session_id; a
fake AcquisitionAppService records stream start/stop and can be told to fail.
"""
from __future__ import annotations

import pytest

from omnibci.application.services.recording_service import (
    RecordingService,
    RecordingConflictError,
    NoActiveRecordingError,
)


class _FakeSessions:
    def __init__(self):
        self.active = None
        self.created = []
        self.ended = []
        self._counter = 0

    @property
    def active_session_id(self):
        return self.active

    def handle_create_session(self, cmd):
        self._counter += 1
        sid = f"sess-{self._counter}"
        self.created.append((sid, cmd.paradigm))
        self.active = sid
        return sid

    def handle_end_session(self, cmd):
        self.ended.append(cmd.session_id)
        if self.active == cmd.session_id:
            self.active = None


class _FakeAcquisition:
    def __init__(self, fail_start=False):
        self.events = []
        self._fail_start = fail_start

    def handle_start_stream(self, cmd):
        if self._fail_start:
            raise RuntimeError("engine not ready")
        self.events.append("start")

    def handle_stop_stream(self, cmd):
        self.events.append("stop")


def _svc(fail_start=False):
    sessions = _FakeSessions()
    acq = _FakeAcquisition(fail_start=fail_start)
    rec = RecordingService(sessions, acq, stop_drain_seconds=0)
    return rec, sessions, acq


class TestStart:
    def test_creates_session_then_starts_stream(self):
        rec, sessions, acq = _svc()
        sid = rec.start(paradigm="classic40SSVEP")
        assert sid == "sess-1"
        assert sessions.created == [("sess-1", "classic40SSVEP")]
        assert acq.events == ["start"]          # stream started after session exists
        assert sessions.ended == []

    def test_conflict_when_already_recording(self):
        rec, sessions, _ = _svc()
        rec.start()
        with pytest.raises(RecordingConflictError):
            rec.start()
        # only one session created
        assert len(sessions.created) == 1

    def test_rolls_back_session_when_stream_fails(self):
        rec, sessions, acq = _svc(fail_start=True)
        with pytest.raises(RuntimeError):
            rec.start()
        assert len(sessions.created) == 1
        assert sessions.ended == ["sess-1"]     # orphan session cleaned up
        assert sessions.active is None
        assert acq.events == []


class TestStop:
    def test_stops_stream_then_ends_session(self):
        rec, sessions, acq = _svc()
        rec.start()
        sid = rec.stop()
        assert sid == "sess-1"
        assert acq.events == ["start", "stop"]
        assert sessions.ended == ["sess-1"]
        assert sessions.active is None

    def test_stop_without_active_raises(self):
        rec, _, _ = _svc()
        with pytest.raises(NoActiveRecordingError):
            rec.stop()

    def test_start_stop_start_is_allowed(self):
        rec, sessions, acq = _svc()
        rec.start()
        rec.stop()
        sid2 = rec.start()
        assert sid2 == "sess-2"
        assert acq.events == ["start", "stop", "start"]
