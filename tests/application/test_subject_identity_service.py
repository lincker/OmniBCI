"""Session subjects survive dispatch, continuation, and recording orchestration."""

from unittest.mock import Mock

import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.queries.session_queries import GetSessionQuery
from omnibci.application.services.recording_service import RecordingService
from omnibci.application.services.session_service import SessionAppService
from omnibci.shared.events import EventType


@pytest.mark.parametrize("subject_id", ["", " \t\n", 123, True, [], {}])
def test_create_command_rejects_invalid_subject(subject_id):
    with pytest.raises(ValueError, match="subject_id"):
        CreateSessionCommand(subject_id=subject_id)


def test_subject_is_dispatched_and_preserved_by_continuation(session_repository):
    broker = Mock()
    sessions = SessionAppService(session_repository, command_results=broker)
    previous = sessions.handle_create_session(CreateSessionCommand(subject_id="subject-123"))
    assert broker.request.call_args.args == (EventType.StartSession, {
        "session_id": previous, "paradigm": None, "subject_id": "subject-123",
    })
    sessions.handle_end_session(EndSessionCommand(previous))
    continued = sessions.create_continuation(previous)

    assert sessions.handle_get_session(GetSessionQuery(continued)).subject_id == "subject-123"
    assert broker.request.call_args.args[1]["subject_id"] == "subject-123"
    assert [item.subject_id for item in sessions.export_snapshot()] == ["subject-123", "subject-123"]


def test_recording_start_binds_subject_to_its_session(session_service):
    acquisition = Mock(spec=["handle_start_stream"])
    recording = RecordingService(session_service, acquisition)

    session_id = recording.start(subject_id="subject-123")

    assert session_service.handle_get_session(GetSessionQuery(session_id)).subject_id == "subject-123"
    acquisition.handle_start_stream.assert_called_once()
