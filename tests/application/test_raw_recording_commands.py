"""Raw recording commands require matching, durable worker receipts."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from omnibci.application.events import AcquisitionSnapshot, CommandResult
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.shared.events import Event, EventType


def _service(data):
    executor = MagicMock()
    executor.request.return_value = CommandResult("command", "recording", True, data=data)
    return AcquisitionAppService(executor), executor


def _receipt(**changes):
    return {"session_id": "session", "id": "recording", "path": "/recordings/recording", **changes}


@pytest.mark.parametrize("ending", [False, True])
def test_recording_commands_wait_under_lifecycle_lock_and_return_detached_receipts(ending):
    response = _receipt(status="completed", metadata={"subject_id": "subject"})
    service, executor = _service(response)
    locked = False

    @contextmanager
    def lifecycle_lock():
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    service.lifecycle_lock = lifecycle_lock()

    def request(event_type, payload):
        assert locked
        assert event_type is (EventType.EndRawRecording if ending else EventType.BeginRawRecording)
        assert payload["session_id"] == "session"
        if not ending:
            payload["metadata"]["subject_id"] = "changed by executor"
        return CommandResult("command", event_type.name, True, data=response)

    executor.request.side_effect = request
    metadata = {"session_id": "session", "metadata": {"subject_id": "subject"}}
    result = service.end_raw_recording("session") if ending else service.begin_raw_recording(metadata)
    assert not locked
    assert result == response
    result["metadata"]["subject_id"] = "changed by caller"
    assert response["metadata"]["subject_id"] == "subject"
    assert metadata["metadata"]["subject_id"] == "subject"
    executor.request.assert_called_once()


def test_begin_accepts_identity_receipt_before_recording_has_finished():
    service, executor = _service(_receipt())
    metadata = {"session_id": "session", "subject_id": None}
    assert service.begin_raw_recording(metadata) == _receipt()
    executor.request.assert_called_once_with(EventType.BeginRawRecording, metadata)


@pytest.mark.parametrize("data", [
    None, [], {},
    _receipt(session_id="other"), _receipt(session_id=None),
    _receipt(id=""), _receipt(id=None), _receipt(id=12),
    _receipt(path=""), _receipt(path=None), _receipt(path=12),
])
@pytest.mark.parametrize("ending", [False, True])
def test_recording_rejects_malformed_or_mismatched_identity(data, ending):
    if isinstance(data, dict):
        data = {**data, "status": "completed"}
    service, _ = _service(data)
    with pytest.raises(RuntimeError, match="recording"):
        if ending:
            service.end_raw_recording("session")
        else:
            service.begin_raw_recording({"session_id": "session"})


@pytest.mark.parametrize("status", [None, "recording", "interrupted", "failed", True])
def test_end_rejects_receipts_that_do_not_confirm_completion(status):
    service, _ = _service(_receipt(status=status))
    with pytest.raises(RuntimeError, match="recording"):
        service.end_raw_recording("session")


@pytest.mark.parametrize("ending", [False, True])
def test_recording_command_failures_are_not_reported_as_success(ending):
    service, executor = _service(_receipt(status="completed"))
    executor.request.side_effect = OSError("Recording disk unavailable")
    with pytest.raises(OSError, match="disk unavailable"):
        if ending:
            service.end_raw_recording("session")
        else:
            service.begin_raw_recording({"session_id": "session"})


def test_streaming_property_follows_worker_state_and_reset():
    service, _ = _service(None)
    assert not service.is_streaming
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, True)))
    assert service.is_streaming
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, False)))
    assert not service.is_streaming
    service.on_acquisition_state(Event(EventType.AcquisitionState, AcquisitionSnapshot(True, True)))
    service.reset()
    assert not service.is_streaming
