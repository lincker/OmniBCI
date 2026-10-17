"""Session commands bind durable raw recording to stopped acquisition boundaries."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import pytest

from omnibci.application.commands.acquisition_commands import StartStreamCommand
from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.events import AcquisitionSnapshot, CommandResult, StreamDrainReceipt, TrialOutcome
from omnibci.application.queries.session_queries import GetSessionQuery
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import CommandTimeoutError
from omnibci.application.services.recording_service import RecordingService
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.classification import Classification
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.shared.events import Event, EventType


class _Runtime:
    """Confirm worker commands while tracking stream and aggregate boundaries."""

    def __init__(self):
        self.events = []
        self.failures = {}
        self.session_id = None
        self.recording = None
        self.completed_trials = 0
        self.on_drain = None
        self.executor = Mock()
        self.executor.request.side_effect = self.request
        self.acquisition = AcquisitionAppService(self.executor)
        self.repository = InMemorySessionRepository()
        self.sessions = SessionAppService(self.repository, command_results=self.executor)
        self.sessions.set_recording_controller(self.acquisition)
        self.acquisition.set_recording_validator(self.sessions.validate_recording_before_stream)

    def stream(self, active):
        self.acquisition.on_acquisition_state(
            Event(EventType.AcquisitionState, AcquisitionSnapshot(True, active, "connection")),
        )

    def request(self, event_type, payload=None, *, timeout_s=None):
        self.events.append((event_type, deepcopy(payload)))
        if event_type in self.failures:
            raise self.failures[event_type]
        data = None
        if event_type is EventType.StopAndDrain:
            self.stream(False)
            if self.on_drain is not None:
                self.on_drain()
            data = StreamDrainReceipt("generation", "revision", 0, self.session_id,
                                      self.completed_trials, False).to_dict()
        elif event_type is EventType.StartSession:
            assert not self.acquisition.is_streaming
            self.session_id = payload["session_id"]
            self.completed_trials = 0
        elif event_type is EventType.BeginRawRecording:
            assert not self.acquisition.is_streaming
            assert self.session_id is None or self.session_id == payload["session_id"]
            session_id = payload["session_id"]
            if (self.recording is not None and self.recording.get("status") != "completed"
                    and self.recording["session_id"] != session_id):
                raise RuntimeError("A recording is already active")
            self.recording = {"session_id": session_id, "id": session_id,
                              "path": f"/recordings/{session_id}"}
            data = deepcopy(self.recording)
        elif event_type is EventType.EndRawRecording:
            assert not self.acquisition.is_streaming
            if self.recording is None or self.recording["session_id"] != payload["session_id"]:
                raise RuntimeError("No recording belongs to this session")
            self.recording["status"] = "completed"
            data = deepcopy(self.recording)
        elif event_type is EventType.EndSession:
            self.session_id = None
        elif event_type is EventType.StartStream:
            if self.session_id is not None:
                assert self.recording is not None
                assert self.recording["session_id"] == self.session_id
            self.stream(True)
        return CommandResult("command", event_type.name, True, data=data)

    def dto(self, session_id):
        return self.sessions.handle_get_session(GetSessionQuery(session_id))

    def project_trial(self, session_id, number):
        self.sessions.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome(
            session_id, number, Classification(target_id=2, event_id=7), "revision",
        )))


@pytest.fixture
def runtime():
    return _Runtime()


@pytest.mark.parametrize("streaming", [False, True])
def test_create_drains_before_start_and_opens_recording_before_resuming(runtime, streaming):
    runtime.stream(streaming)
    sid = runtime.sessions.handle_create_session(CreateSessionCommand(paradigm="p", subject_id="subject-1"))
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.StartSession, EventType.BeginRawRecording,
        *([EventType.StartStream] if streaming else []),
    ]
    assert runtime.events[1][1] == {"session_id": sid, "paradigm": "p", "subject_id": "subject-1"}
    assert runtime.events[2][1] == {"session_id": sid, "subject_id": "subject-1",
                                    "paradigm": "p", "continuation_of": None}
    dto = runtime.dto(sid)
    assert dto.subject_id == "subject-1"
    assert dto.recording == {**runtime.recording, "status": "recording"}
    assert runtime.acquisition.is_streaming is streaming


def test_recording_service_start_uses_session_recorder_before_starting_stream(runtime):
    recording = RecordingService(runtime.sessions, runtime.acquisition)
    sid = recording.start("p", subject_id="subject")
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.StartSession, EventType.BeginRawRecording, EventType.StartStream,
    ]
    assert runtime.dto(sid).subject_id == "subject"
    assert runtime.acquisition.is_streaming


def test_managed_recording_stop_drains_once_and_finalizes_before_ending_session(runtime):
    recording = RecordingService(runtime.sessions, runtime.acquisition)
    sid = recording.start()
    runtime.events.clear()
    assert recording.stop() == sid
    assert runtime.events == [
        (EventType.StopAndDrain, {"session_id": sid}),
        (EventType.EndRawRecording, {"session_id": sid}),
        (EventType.EndSession, sid),
    ]
    assert runtime.dto(sid).state == "ended"
    assert runtime.dto(sid).recording["status"] == "completed"


def test_managed_recording_stop_retry_after_lost_end_ack_does_not_redrain_ended_worker(runtime):
    recording = RecordingService(runtime.sessions, runtime.acquisition)
    sid = recording.start()
    runtime.failures[EventType.EndSession] = CommandTimeoutError("late", "EndSession", 0.1)
    runtime.events.clear()
    with pytest.raises(CommandTimeoutError):
        recording.stop()
    assert runtime.events == [
        (EventType.StopAndDrain, {"session_id": sid}),
        (EventType.EndRawRecording, {"session_id": sid}),
        (EventType.EndSession, sid),
    ]
    assert runtime.dto(sid).state == "running"
    assert runtime.dto(sid).recording["status"] == "completed"
    # The aggregate ended, but neither its event nor acknowledgement reached the API.
    runtime.session_id = None
    del runtime.failures[EventType.EndSession]
    runtime.events.clear()
    assert recording.stop() == sid
    assert runtime.events == [(EventType.EndSession, sid)]
    assert runtime.dto(sid).state == "ended"
    assert runtime.dto(sid).recording["status"] == "completed"


def test_end_projects_final_drained_trial_and_finalizes_file_before_ending_aggregate(runtime):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    runtime.acquisition.handle_start_stream(StartStreamCommand())
    runtime.completed_trials = 1
    runtime.on_drain = lambda: runtime.project_trial(sid, 1)
    runtime.events.clear()
    runtime.sessions.handle_end_session(EndSessionCommand(sid))
    assert runtime.events == [
        (EventType.StopAndDrain, {"session_id": sid}),
        (EventType.EndRawRecording, {"session_id": sid}),
        (EventType.EndSession, sid),
    ]
    dto = runtime.dto(sid)
    assert dto.state == "ended"
    assert dto.trial_count == 1
    assert dto.recording["status"] == "completed"
    assert not runtime.acquisition.is_streaming


@pytest.mark.parametrize("trial_numbers", [[], [2], [1, 1]])
def test_missing_or_duplicate_trial_projection_prevents_file_and_session_finalization(runtime, trial_numbers):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    runtime.completed_trials = 2
    for number in trial_numbers:
        runtime.project_trial(sid, number)
    runtime.events.clear()
    with pytest.raises(RuntimeError, match="projection is incomplete"):
        runtime.sessions.handle_end_session(EndSessionCommand(sid))
    assert runtime.events == [(EventType.StopAndDrain, {"session_id": sid})]
    assert runtime.dto(sid).state == "running"
    assert runtime.dto(sid).recording["status"] == "recording"


def test_switching_running_session_closes_old_recording_before_new_recording_and_resume(runtime):
    old = runtime.sessions.handle_create_session(CreateSessionCommand(subject_id="first"))
    runtime.acquisition.handle_start_stream(StartStreamCommand())
    runtime.events.clear()
    new = runtime.sessions.handle_create_session(CreateSessionCommand(subject_id="second"))
    assert new != old
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.EndRawRecording, EventType.EndSession,
        EventType.StartSession, EventType.BeginRawRecording, EventType.StartStream,
    ]
    assert runtime.events[0][1] == {"session_id": old}
    assert runtime.events[1][1] == {"session_id": old}
    assert runtime.events[2][1] == old
    assert runtime.events[3][1]["session_id"] == new
    assert runtime.events[4][1]["session_id"] == new
    assert runtime.dto(old).state == "ended"
    assert runtime.dto(old).recording["status"] == "completed"
    assert runtime.dto(new).state == "running"
    assert runtime.dto(new).recording["status"] == "recording"
    assert runtime.acquisition.is_streaming


def test_continuation_opens_new_recording_and_inherits_subject(runtime):
    old = runtime.sessions.handle_create_session(CreateSessionCommand(subject_id="subject"))
    runtime.sessions.handle_end_session(EndSessionCommand(old, "configuration_changed"))
    new = runtime.sessions.create_continuation(old, config_revision="new-revision")
    assert new != old
    assert runtime.events[-1] == (EventType.BeginRawRecording, {
        "session_id": new, "subject_id": "subject", "paradigm": None, "continuation_of": old,
    })
    assert runtime.dto(new).continuation_of == old
    assert runtime.dto(new).subject_id == "subject"
    assert runtime.dto(new).recording["path"] != runtime.dto(old).recording["path"]


def test_begin_recording_failure_ends_new_aggregate_without_resuming_or_claiming_file_completion(runtime):
    runtime.stream(True)
    runtime.failures[EventType.BeginRawRecording] = OSError("Disk unavailable")
    with pytest.raises(OSError, match="Disk unavailable"):
        runtime.sessions.handle_create_session(CreateSessionCommand())
    record, = runtime.repository.find_all()
    assert record.state == "ended"
    assert record.recording == {"session_id": record.session_id, "status": "failed"}
    assert record.interruption_reason == "Raw recording could not be started"
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.StartSession, EventType.BeginRawRecording,
        EventType.EndRawRecording, EventType.EndSession,
    ]
    assert not runtime.acquisition.is_streaming


def test_malformed_begin_receipt_closes_opened_file_before_ending_session_and_allows_next_session(runtime):
    def malformed_begin(event_type, payload=None, *, timeout_s=None):
        result = runtime.request(event_type, payload, timeout_s=timeout_s)
        if event_type is EventType.BeginRawRecording:
            return replace(result, data={"session_id": payload["session_id"]})
        return result

    runtime.executor.request.side_effect = malformed_begin
    with pytest.raises(RuntimeError, match="invalid raw recording evidence"):
        runtime.sessions.handle_create_session(CreateSessionCommand())
    record, = runtime.repository.find_all()
    assert record.state == "ended"
    assert record.recording["status"] == "completed"
    assert record.recording["id"] == runtime.recording["id"]
    assert record.recording["path"] == runtime.recording["path"]
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.StartSession, EventType.BeginRawRecording,
        EventType.EndRawRecording, EventType.EndSession,
    ]
    assert not runtime.acquisition.is_streaming
    runtime.executor.request.side_effect = runtime.request
    next_id = runtime.sessions.handle_create_session(CreateSessionCommand())
    assert next_id != record.session_id
    assert runtime.dto(next_id).recording["status"] == "recording"


@pytest.mark.parametrize("event_type", [EventType.StartSession, EventType.BeginRawRecording])
def test_unknown_start_outcome_retains_session_without_resuming_or_claiming_recording_completed(runtime, event_type):
    runtime.stream(True)
    runtime.failures[event_type] = CommandTimeoutError("unknown", event_type.name, 0.1)
    with pytest.raises(CommandTimeoutError):
        runtime.sessions.handle_create_session(CreateSessionCommand())
    record, = runtime.repository.find_all()
    assert record.state == "running"
    assert record.recording["status"] == ("recording" if event_type is EventType.StartSession else "pending")
    assert EventType.StartStream not in [event for event, _ in runtime.events]
    assert EventType.EndSession not in [event for event, _ in runtime.events]
    assert not runtime.acquisition.is_streaming


def test_late_start_confirmation_keeps_armed_recording_and_allows_end(runtime):
    runtime.failures[EventType.StartSession] = CommandTimeoutError("late", "StartSession", 0.1)
    with pytest.raises(CommandTimeoutError):
        runtime.sessions.handle_create_session(CreateSessionCommand())
    record, = runtime.repository.find_all()
    assert record.recording["status"] == "recording"
    assert runtime.events[-1][0] is EventType.BeginRawRecording
    runtime.session_id = record.session_id
    runtime.sessions.on_session_started(Event(EventType.SessionStarted, record.session_id))
    runtime.events.clear()
    runtime.sessions.handle_end_session(EndSessionCommand(record.session_id))
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.EndRawRecording, EventType.EndSession,
    ]
    assert runtime.dto(record.session_id).state == "ended"
    assert runtime.dto(record.session_id).recording["status"] == "completed"


def test_pending_begin_is_retried_before_finalizing_and_ending(runtime):
    runtime.failures[EventType.BeginRawRecording] = CommandTimeoutError("late", "BeginRawRecording", 0.1)
    with pytest.raises(CommandTimeoutError):
        runtime.sessions.handle_create_session(CreateSessionCommand(subject_id="subject"))
    record, = runtime.repository.find_all()
    initial_begin = runtime.events[-1]
    del runtime.failures[EventType.BeginRawRecording]
    runtime.events.clear()
    runtime.sessions.handle_end_session(EndSessionCommand(record.session_id))
    assert [event for event, _ in runtime.events] == [
        EventType.StopAndDrain, EventType.BeginRawRecording, EventType.EndRawRecording, EventType.EndSession,
    ]
    assert runtime.events[1] == initial_begin
    assert runtime.dto(record.session_id).state == "ended"
    assert runtime.dto(record.session_id).recording["status"] == "completed"


def test_end_timeout_keeps_session_running_with_closed_recording_and_blocks_resume_until_retry(runtime):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    runtime.failures[EventType.EndSession] = CommandTimeoutError("late", "EndSession", 0.1)
    with pytest.raises(CommandTimeoutError):
        runtime.sessions.handle_end_session(EndSessionCommand(sid))
    assert runtime.dto(sid).state == "running"
    assert runtime.dto(sid).recording["status"] == "completed"
    runtime.events.clear()
    with pytest.raises(RuntimeError, match="no writable raw recording"):
        runtime.acquisition.handle_start_stream(StartStreamCommand())
    assert runtime.events == []
    del runtime.failures[EventType.EndSession]
    # The worker may have ended even though the acknowledgement was lost.
    runtime.session_id = None
    runtime.sessions.handle_end_session(EndSessionCommand(sid))
    assert runtime.events == [(EventType.EndSession, sid)]
    assert runtime.dto(sid).state == "ended"
    assert runtime.dto(sid).recording["status"] == "completed"


@pytest.mark.parametrize("reason", [None, "source_replacement"])
def test_disconnected_recording_ends_after_projection_check_without_claiming_completed(runtime, reason):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    info = {**runtime.recording, "status": "interrupted"}
    runtime.sessions.on_raw_recording_state(Event(EventType.RawRecordingState, info))
    runtime.completed_trials = 1
    runtime.on_drain = lambda: runtime.project_trial(sid, 1)
    runtime.events.clear()
    runtime.sessions.handle_end_session(EndSessionCommand(sid, interruption_reason=reason))
    expected_reason = reason or "Raw recording interrupted by source disconnect"
    assert runtime.events == [
        (EventType.StopAndDrain, {"session_id": sid}),
        (EventType.EndSession, {"session_id": sid, "interruption_reason": expected_reason}),
    ]
    dto = runtime.dto(sid)
    assert dto.state == "ended"
    assert dto.recording["status"] == "interrupted"
    assert dto.interruption_reason == expected_reason
    assert dto.trial_count == 1


def test_disconnected_recording_with_missing_trials_is_retained_for_reconciliation(runtime):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    runtime.sessions.on_raw_recording_state(Event(EventType.RawRecordingState, {
        **runtime.recording, "status": "interrupted",
    }))
    runtime.completed_trials = 1
    runtime.events.clear()
    with pytest.raises(RuntimeError, match="projection is incomplete"):
        runtime.sessions.handle_end_session(EndSessionCommand(sid))
    assert runtime.events == [(EventType.StopAndDrain, {"session_id": sid})]
    assert runtime.dto(sid).state == "running"
    assert runtime.dto(sid).recording["status"] == "interrupted"


@pytest.mark.parametrize("status", [None, "pending", "failed", "interrupted", "completed"])
def test_stream_validator_rejects_active_session_without_writable_recording_before_preparation(runtime, status):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    recording = None if status is None else {**runtime.recording, "status": status}
    runtime.repository.update(sid, lambda record: setattr(record, "recording", recording))
    prepare = Mock()
    runtime.acquisition.set_signal_preparer(prepare)
    runtime.events.clear()
    with pytest.raises(RuntimeError, match="no writable raw recording"):
        runtime.acquisition.handle_start_stream(StartStreamCommand())
    prepare.assert_not_called()
    assert runtime.events == []
    assert not runtime.acquisition.is_streaming


def test_stream_validator_preserves_standalone_stream_without_session_or_recording(runtime):
    runtime.acquisition.handle_start_stream(StartStreamCommand())
    assert runtime.events == [(EventType.StartStream, None)]
    assert runtime.acquisition.is_streaming
    assert runtime.recording is None


@pytest.mark.parametrize("event_type", [EventType.StopAndDrain, EventType.EndRawRecording])
@pytest.mark.parametrize("failure", [OSError("Disk sync failed"), CommandTimeoutError("unknown", "recording", 0.1)])
def test_end_failure_keeps_session_open_without_claiming_recording_completed(runtime, event_type, failure):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    runtime.events.clear()
    runtime.failures[event_type] = failure
    with pytest.raises(type(failure)):
        runtime.sessions.handle_end_session(EndSessionCommand(sid))
    assert EventType.EndSession not in [event for event, _ in runtime.events]
    assert runtime.dto(sid).state == "running"
    assert runtime.dto(sid).recording["status"] == "recording"


def test_late_raw_recording_state_reconciles_begin_timeout_without_ending_session(runtime):
    runtime.failures[EventType.BeginRawRecording] = CommandTimeoutError("late", "BeginRawRecording", 0.1)
    with pytest.raises(CommandTimeoutError):
        runtime.sessions.handle_create_session(CreateSessionCommand(subject_id="subject"))
    record, = runtime.repository.find_all()
    info = {"id": "recording", "session_id": record.session_id,
            "path": "/recordings/recording", "status": "recording"}
    runtime.sessions.on_raw_recording_state(Event(EventType.RawRecordingState, info))
    info["status"] = "changed by producer"
    dto = runtime.dto(record.session_id)
    assert dto.recording["status"] == "recording"
    assert dto.state == "running"
    dto.recording["status"] = "changed by caller"
    assert runtime.dto(record.session_id).recording["status"] == "recording"


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
def test_late_recording_final_state_updates_only_its_session_recording(runtime, status):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    info = {**runtime.recording, "status": status}
    runtime.sessions.on_raw_recording_state(Event(EventType.RawRecordingState, info))
    dto = runtime.dto(sid)
    assert dto.recording == info
    assert dto.state == "running"


@pytest.mark.parametrize("payload", [None, "recording", {}, {"session_id": 3}, {"session_id": "unknown"}])
def test_unidentified_recording_state_does_not_create_or_rewrite_sessions(runtime, payload):
    sid = runtime.sessions.handle_create_session(CreateSessionCommand())
    before = runtime.dto(sid)
    runtime.sessions.on_raw_recording_state(Event(EventType.RawRecordingState, payload))
    assert runtime.dto(sid) == before
    assert len(runtime.repository.find_all()) == 1
