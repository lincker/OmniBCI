"""Worker session commands retain subjects and reject invalid identity changes."""

from unittest.mock import MagicMock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.shared.events import EventType


def _processor():
    return SignalProcessor(
        detector=MagicMock(), decoder=MagicMock(), command_queue=MagicMock(),
        data_manager=MagicMock(), event_bus=MagicMock(), auto_start_session=False,
    )


def test_worker_keeps_subject_and_rejects_change_on_same_identity():
    worker = _processor()
    payload = {"session_id": "session", "subject_id": "subject-123"}
    assert worker._start_session(payload)
    session = worker.session

    assert session.subject_id == "subject-123"
    assert worker._start_session(payload)
    assert not worker._start_session({**payload, "subject_id": "different"})
    assert worker.session is session
    assert worker.session.subject_id == "subject-123"


@pytest.mark.parametrize("subject_id", ["", " \t\n", 123, True, [], {}])
def test_invalid_subject_command_is_rejected_without_replacing_session(subject_id):
    worker = _processor()
    assert worker._start_session({"session_id": "previous", "subject_id": "subject-123"})
    previous = worker.session

    worker._execute((SignalProcessorCommand.start_session, CommandRequest(
        "invalid-subject", {"session_id": "new", "subject_id": subject_id},
    )))

    assert worker.session is previous
    result = worker.event_bus.put_event.call_args.args[0]
    assert result.type == EventType.CommandResult
    assert result.data.ok is False
