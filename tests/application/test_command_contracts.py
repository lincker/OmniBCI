"""Command admission and completion use explicit, closed payload contracts."""
import pytest

from omnibci.application.events import CommandResult, StreamDrainReceipt
from omnibci.application.services.command_result_service import CommandExecutionError, CommandResultBroker
from omnibci.shared.events import Event, EventType
from tests.application.test_command_result_service import _Engine


@pytest.mark.parametrize("event_type,payload", [
    (EventType.ConnectDataProvider, {"ignored": True}),
    (EventType.GetSourceMetadata, {"refresh": "yes"}),
    (EventType.AttachProcessingInput, {"input_id": "unprepared"}),
    (EventType.DetachProcessingInput, {"generation": "g", "engine_revision": "e", "expected_revision": "a", "drain": "false"}),
    (EventType.PrepareSignalProcessing, {"plan": {}, "receipt": {}}),
    (EventType.PauseStimulation, {"ignored": True}),
    (EventType.SettleActuation, {"ignored": True}),
    (EventType.StartStimTrial, {"n_trials": True, "trial_params": [{}]}),
    (EventType.TrialCompleted, None),
    (EventType.AcquisitionState, None),
])
def test_invalid_command_never_reaches_execution(event_type, payload):
    engine = _Engine()
    broker = CommandResultBroker(engine)
    engine.on_dispatch = lambda event: broker.on_result(Event(EventType.CommandResult,
        CommandResult(event.data.command_id, event.type.name, True)))
    with pytest.raises(ValueError):
        broker.request(event_type, payload)
    assert not engine.dispatched
    assert broker.pending_count == 0


@pytest.mark.parametrize("event_type,data", [
    (EventType.StopAndDrain, {"outputs_settled": True}),
    (EventType.PauseStimulation, {"paused": True}),
    (EventType.SettleActuation, {"paused": True, "execution_complete": True, "outputs_settled": False}),
])
def test_malformed_success_is_an_execution_failure_not_confirmation(event_type, data):
    engine = _Engine()
    broker = CommandResultBroker(engine)
    engine.on_dispatch = lambda event: broker.on_result(Event(EventType.CommandResult,
        CommandResult(event.data.command_id, event.type.name, True, data=data)))
    with pytest.raises(CommandExecutionError) as raised:
        broker.request(event_type)
    assert raised.value.result.error_code == "INVALID_COMMAND_RESULT"
    assert len(engine.dispatched) == 1
    assert broker.pending_count == 0


def test_accepted_result_is_detached_from_the_event_producer():
    engine = _Engine()
    broker = CommandResultBroker(engine)
    data = StreamDrainReceipt("g", "a", 1, 2, False, processing_revision="p", engine_revision="e").to_dict()
    def respond(event):
        broker.on_result(Event(EventType.CommandResult,
            CommandResult(event.data.command_id, event.type.name, True, data=data)))
        data["completed_trials"] = 99
    engine.on_dispatch = respond
    assert broker.request(EventType.StopAndDrain).data["completed_trials"] == 2


def test_queries_and_cross_module_attachment_have_explicit_owners():
    from omnibci.application.command_contracts import command_contract

    for query in (EventType.GetSourceMetadata, EventType.GetAcquisitionPreview):
        assert command_contract(query).owner == "acquisition"
        assert command_contract(query).scopes == ()
    for event_type in (EventType.AttachProcessingInput, EventType.DetachProcessingInput):
        assert command_contract(event_type).owner == "acquisition"
        assert command_contract(event_type).scopes == ("processing",)
    with pytest.raises(ValueError):
        command_contract(EventType.CommandResult)
    with pytest.raises(ValueError):
        command_contract(199)
