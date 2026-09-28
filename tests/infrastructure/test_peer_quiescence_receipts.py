"""Correlated pause/settle evidence survives the peer's JSON wire boundary."""
import pytest

from omnibci.application.events import CommandResult
from omnibci.infrastructure.ipc.contracts import PROTOCOL_VERSION, body_to_event, event_to_body
from omnibci.shared.events import Event, EventType


@pytest.mark.parametrize("command,evidence", [
    (EventType.PauseStimulation, {"paused": True, "renderer_stopped": True, "cancelled_trials": 2}),
    (EventType.SettleActuation, {"paused": True, "execution_complete": True, "outputs_settled": True}),
])
def test_command_result_preserves_quiescence_evidence(command, evidence):
    data = {**evidence, "config_revision": "revision", "generation": "generation"}
    result = CommandResult("correlation", command.name, ok=True, data=data)
    received = body_to_event(event_to_body(Event(EventType.CommandResult, result)).encode(), PROTOCOL_VERSION)
    assert received.type is EventType.CommandResult
    assert CommandResult.from_value(received.data) == result
