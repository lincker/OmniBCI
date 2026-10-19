"""Failed sessions close only after their runtime generation has stopped."""

from datetime import datetime, timezone
from threading import RLock
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.application.commands.session_commands import AbortSessionCommand, CreateSessionCommand
from omnibci.application.events import StreamDrainReceipt, SessionSummary
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.repositories import SessionRecord, SessionRecordState
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.shared.events import Event, EventType


@pytest.fixture
def context():
    repository = InMemorySessionRepository()
    repository.save(SessionRecord("session", None, SessionRecordState.RUNNING, datetime.now(timezone.utc),
                                  recording={"session_id": "session", "status": "failed", "generation": "old"}))
    engine = Mock(handle_stop_engine=AsyncMock())
    acquisition = Mock(lifecycle_lock=RLock())
    acquisition.handle_stop_and_drain.return_value = StreamDrainReceipt("old", "revision", 0, "session", 0, False)
    acquisition.abort_raw_recording.return_value = {"session_id": "session", "status": "failed"}
    admission = RuntimeAdmission()
    service = SessionAppService(repository, engine_port=engine, admission=admission)
    service.set_recording_controller(acquisition)
    return service, repository, engine, acquisition, admission


@pytest.mark.asyncio
async def test_failed_writer_can_be_aborted_and_repeated_without_stopping_new_runtime(context):
    service, repository, engine, acquisition, admission = context
    acquisition.handle_stop_and_drain.side_effect = RuntimeError("disk full")
    await service.handle_abort_session(AbortSessionCommand("session", "Disk full"))
    record = repository.find_by_id("session")
    assert record.state is SessionRecordState.ENDED
    assert record.interruption_reason == "Disk full"
    assert record.recording["status"] == "failed"
    assert record.recording["abort_errors"] == ["Drain: disk full"]
    assert record.recording["abort_pending"] is False
    assert not admission.recovery_required
    acquisition.reset.assert_called_once()
    await service.handle_abort_session(AbortSessionCommand("session", "Retry"))
    engine.handle_stop_engine.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_status", ["failed", "completed"])
async def test_abort_stop_timeout_keeps_admission_closed_and_retryable_after_end_event(context, raw_status):
    service, repository, engine, acquisition, admission = context
    acquisition.abort_raw_recording.return_value["status"] = raw_status

    async def stop():
        service.on_session_ended(Event(EventType.SessionEnded, SessionSummary("session", 0)))
        raise RuntimeError("worker still alive")

    async def failing_stop(command):
        await stop()
    engine.handle_stop_engine.side_effect = failing_stop
    with pytest.raises(RuntimeError, match="still alive"):
        await service.handle_abort_session(AbortSessionCommand("session", "Failed stop"))
    assert repository.find_by_id("session").recording["abort_pending"]
    assert admission.recovery_required
    with pytest.raises(ProfileRevisionConflict, match="requires recovery"):
        service.handle_create_session(CreateSessionCommand())
    engine.handle_stop_engine.side_effect = None
    await service.handle_abort_session(AbortSessionCommand("session", "Retry"))
    assert engine.handle_stop_engine.await_count == 2
    assert not admission.recovery_required


@pytest.mark.asyncio
async def test_ended_session_with_unfinished_recording_still_stops_and_recovers(context):
    service, repository, engine, acquisition, admission = context
    repository.update("session", lambda record: record.mark_ended())
    ended_at = repository.find_by_id("session").ended_at
    repository.update("session", lambda record: record.recording.update(status="pending"))
    recovered = Mock(return_value={"session_id": "session", "status": "interrupted"})
    service.set_recording_recovery(recovered)
    await service.handle_abort_session(AbortSessionCommand("session", "Recover unfinished file"))
    engine.handle_stop_engine.assert_awaited_once()
    recovered.assert_called_once()
    assert repository.find_by_id("session").ended_at == ended_at


@pytest.mark.asyncio
async def test_abort_reconciles_file_only_after_confirmed_engine_stop(context):
    service, repository, engine, acquisition, admission = context
    repository.update("session", lambda r: r.recording.update(path="/recordings/session"))
    calls = []

    async def stopped(command):
        calls.append("stopped")

    def recover(record):
        assert calls == ["stopped"]
        return {**record.recording, "status": "failed", "chunk_count": 4}

    engine.handle_stop_engine.side_effect = stopped
    service.set_recording_recovery(recover)
    acquisition.abort_raw_recording.side_effect = RuntimeError("flush failed")
    await service.handle_abort_session(AbortSessionCommand("session", "Write failed"))
    assert repository.find_by_id("session").recording["chunk_count"] == 4


def test_terminal_recording_cannot_be_reopened_by_late_or_wrong_generation_events(context):
    service, repository, *_ = context
    for status, generation in (("recording", "old"), ("completed", "old"), ("failed", "other")):
        service.on_raw_recording_state(Event(EventType.RawRecordingState, {
            "session_id": "session", "status": status, "generation": generation, "path": "wrong",
        }))
    assert repository.find_by_id("session").recording == {
        "session_id": "session", "status": "failed", "generation": "old",
    }


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_abort_requires_a_reason(reason):
    with pytest.raises(ValueError, match="reason"):
        AbortSessionCommand("session", reason)
