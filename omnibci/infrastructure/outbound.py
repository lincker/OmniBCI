"""Serialize peer payloads and construct output-host acknowledgements."""

from copy import deepcopy
from typing import Any, Optional

from omnibci.application.commands.actuation_commands import ActuationIntent, InvalidActuationCommand
from omnibci.application import events as application_events


def make_stimulation_ack(command: str, status: str = "ok",
                         trial_params: Optional[dict[str, object]] = None,
                         message: Optional[str] = None) -> dict:
    """
    Build the JSON-safe payload the stimulation process sends back to ack a
    command.  Used on the stimulation side as::

        client.send_event(Event(EventType.StimulationAck,
                                 make_stimulation_ack("StartStimTrial",
                                                      trial_params={"event_id": 3})))
    """
    return {"command": command, "status": status,
            "trial_params": deepcopy(trial_params), "message": message}


def make_actuation_ack(command: ActuationIntent, status: str = "ok", message: str | None = None) -> dict:
    """Return the same command identity and trial origin for every outcome."""
    return {**command.correlation, "status": status, "message": message}


def make_actuation_fault(code: str, *, command: ActuationIntent | InvalidActuationCommand | None = None,
                         message: str | None = None) -> dict:
    """Device-wide faults have no command; rejected commands echo valid identity."""
    correlation = command.correlation if command is not None else {
        "command_id": None, "origin": None, "target_id": None,
    }
    return {"code": code, "source": "actuation_subsystem", **correlation, "message": message}


def payload_to_jsonable(data: Any) -> Any:
    """
    Convert an EventBus payload into a JSON-serialisable value for the socket
    envelope (``{"type", "data"}``).  Rich domain payloads are flattened; plain
    scalars pass through unchanged.
    """
    if isinstance(data, ActuationIntent):
        return data.to_dict()
    if isinstance(data, application_events.TrialOutcome):
        return data.to_dict()
    if isinstance(data, application_events.CommandRequest):
        return {
            "command_id": data.command_id,
            "payload": payload_to_jsonable(data.payload),
        }
    if isinstance(data, application_events.CommandResult):
        return {
            "command_id": data.command_id,
            "command": data.command,
            "ok": data.ok,
            "message": data.message,
            "error_code": data.error_code,
            "failure_kind": data.failure_kind,
            **({"data": payload_to_jsonable(data.data)} if data.data is not None else {}),
        }
    return data
