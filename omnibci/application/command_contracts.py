"""Closed v1 command ownership and payload/result codecs, independent of transport."""
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from typing import Callable

from omnibci.application.acquisition_preview import PreviewSelection, limits
from omnibci.application.commands.actuation_commands import validate_actuation_command
from omnibci.application.commands.stimulation_commands import StartStimTrialCommand
from omnibci.application.events import CommandRequest, SignalPreparationReceipt, StreamDrainReceipt
from omnibci.application.plugin_schema import validate_parameters
from omnibci.application.source_checkpoint import checkpoint_from_receipt
from omnibci.application.source_metadata import metadata_receipt
from omnibci.domain.channel_mapping import ResolvedSignalPlan
from omnibci.domain.ports.acquisition import SourceCheckpoint
from omnibci.shared.events import Event, EventType


def _record(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("Command value has missing or unknown fields")
    return deepcopy(value)


def _text(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("Command identity must be non-empty text")


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"Command count must be an integer >= {minimum}")


def _none(value):
    if value is not None:
        raise ValueError("This command requires no payload")


def _identity(value, *, extra=(), optional=()):
    fields = {"generation", "engine_revision", "expected_revision"}
    result = _record(value, fields | set(extra), optional)
    for key in fields:
        _text(result[key])
    return result


def _checkpoint(value):
    data = _identity(value, extra={"checkpoint"})
    checkpoint = _record(data["checkpoint"], {"resource_id", "resource_revision", "state"})
    SourceCheckpoint(**checkpoint)
    return data


def metadata_request(value):
    if value is None:
        return None
    data = _record(value, (), {"refresh"})
    if "refresh" in data and type(data["refresh"]) is not bool:
        raise ValueError("refresh must be a boolean")
    return data


def _source_receipt(value, *, restore=False):
    fields = {"generation", "engine_revision", "acquisition_revision"}
    data = _record(value, fields | ({"restored"} if restore else {"resume_mode", "checkpoint"}))
    for key in fields:
        _text(data[key])
    if restore:
        if data["restored"] is not True:
            raise ValueError("Source did not confirm checkpoint restoration")
    else:
        checkpoint_from_receipt(data)
    return data


def _preview_request(value, *, action):
    fields = {"generation"}
    if action == "start":
        fields |= {"expected_revision", "connection_id", "capture_id"}
    elif action == "stop":
        fields.add("capture_id")
    optional = {"selection", "continuous"} if action == "start" else {"capture_id"} if action == "get" else ()
    data = _record(value, fields, optional)
    for key in fields - {"selection", "continuous"}:
        _text(data[key])
    if data.get("capture_id") is not None and (not isinstance(data["capture_id"], str) or not 1 <= len(data["capture_id"]) <= 128):
        raise ValueError("Invalid preview capture_id")
    if action == "start":
        data["selection"] = PreviewSelection.from_value(data.get("selection", {})).to_dict()
        data.setdefault("continuous", False)
        if type(data["continuous"]) is not bool:
            raise ValueError("continuous must be a boolean")
    return data


def _preview_receipt(value):
    identities = {"capture_id", "generation", "acquisition_revision", "engine_revision", "source_id", "connection_id"}
    counts = {"observed", "enqueued", "dropped", "oversized", "enqueued_bytes", "stream_chunk_count", "stream_sample_count"}
    data = _record(value, identities | counts | {"status", "reason", "continuous", "selection", "limits", "last_sequence"})
    for key in identities:
        if data[key] is not None:
            _text(data[key])
    for key in counts:
        _integer(data[key])
    _integer(data["last_sequence"], -1)
    if (data["status"] not in {"idle", "capturing", "finished"}
            or type(data["continuous"]) is not bool
            or data["reason"] is not None and not isinstance(data["reason"], str)
            or data["limits"] != limits()):
        raise ValueError("Invalid preview status receipt")
    if data["selection"] is not None:
        PreviewSelection.from_value(data["selection"])
    return data


def prepare_signal_request(value):
    data = _record(value, {"plan", "receipt"}, {"settings_snapshot"})
    plan = ResolvedSignalPlan.from_dict(data["plan"])
    receipt = SignalPreparationReceipt.from_value(data["receipt"])
    if receipt.plan_fingerprint != plan.fingerprint or receipt.acquisition_revision != plan.acquisition_revision:
        raise ValueError("Signal preparation does not match its plan")
    if "settings_snapshot" in data:
        data["settings_snapshot"] = validate_parameters(None, data["settings_snapshot"])
    data["receipt"] = receipt.to_dict()
    return data


def _preparation_receipt(value):
    return SignalPreparationReceipt.from_value(value).to_dict()


def _attachment_receipt(value):
    fields = set(SignalPreparationReceipt.__dataclass_fields__)
    data = _record(value, fields | {"attached", "last_sequence"})
    SignalPreparationReceipt.from_value({key: data[key] for key in fields})
    _integer(data["last_sequence"], -1)
    if data["attached"] is not True:
        raise ValueError("Processing did not confirm input attachment")
    return data


def detach_input_request(value):
    data = _identity(value, optional={"drain"})
    if "drain" in data and type(data["drain"]) is not bool:
        raise ValueError("drain must be a boolean")
    return data


def _drain_receipt(value):
    return StreamDrainReceipt.from_value(value).to_dict()


def reset_input_request(value):
    data = _record(value, {"generation", "engine_revision"}, {"expected_revision", "expected_acquisition_revision", "input_id"})
    if len(data.keys() & {"expected_revision", "expected_acquisition_revision"}) != 1:
        raise ValueError("Input reset requires exactly one owning revision")
    for item in data.values():
        _text(item)
    return data


def _reset_receipt(value):
    data = _record(value, {"generation", "engine_revision", "processing_revision", "attached"})
    for key in ("generation", "engine_revision", "processing_revision"):
        _text(data[key])
    if data["attached"] is not False:
        raise ValueError("Processing did not confirm input reset")
    return data


def stage_configuration_request(value):
    data = _identity(value, extra={"settings_snapshot", "boundary"})
    if data["boundary"] not in {"trial", "module"}:
        raise ValueError("Unknown processing update boundary")
    data["settings_snapshot"] = validate_parameters(None, data["settings_snapshot"])
    return data


def cancel_configuration_request(value):
    data = _record(value, {"command_id", "generation", "engine_revision"})
    for item in data.values():
        _text(item)
    return data


def _configuration_receipt(value):
    data = _record(value, {"status", "command_id", "applied_revision", "generation", "engine_revision"})
    for key in ("command_id", "applied_revision", "generation", "engine_revision"):
        _text(data[key])
    if data["status"] not in {"applied", "cancelled"}:
        raise ValueError("Invalid processing configuration outcome")
    return data


def stimulation_trial_request(value):
    data = _record(value, {"trial_params"}, {"n_trials"})
    command = StartStimTrialCommand(**data)
    return {"n_trials": command.n_trials, "trial_params": command.trial_params}


def _output_receipt(value, *, flags, counts=()):
    fields = {"generation", "module_revision", "engine_revision"}
    data = _record(value, fields | set(flags) | set(counts))
    for key in fields:
        _text(data[key])
    for key, expected in flags.items():
        if data[key] is not expected:
            raise ValueError(f"Output did not confirm {key}")
    for key in counts:
        _integer(data[key])
    return data


def _actuation_request(value):
    return validate_actuation_command(value)


@dataclass(frozen=True)
class CommandContract:
    owner: str
    scopes: tuple[str, ...]
    parse_payload: Callable
    parse_result: Callable | None


_CONTRACTS = {
    EventType.ConnectDataProvider: CommandContract("acquisition", ("acquisition",), _none, _none),
    EventType.DisconnectDataProvider: CommandContract("acquisition", ("acquisition",), _none, _none),
    EventType.GetSourceCheckpoint: CommandContract("acquisition", ("acquisition",), _identity, _source_receipt),
    EventType.RestoreSourceCheckpoint: CommandContract("acquisition", ("acquisition",), _checkpoint, partial(_source_receipt, restore=True)),
    EventType.GetSourceMetadata: CommandContract("acquisition", (), metadata_request, metadata_receipt),
    EventType.StartStream: CommandContract("acquisition", ("acquisition",), _none, _none),
    EventType.StopStream: CommandContract("acquisition", ("acquisition",), _none, _none),
    EventType.StopAndDrain: CommandContract("acquisition", ("acquisition",), _none, _drain_receipt),
    EventType.StartAcquisitionPreview: CommandContract("acquisition", ("acquisition",), partial(_preview_request, action="start"), _preview_receipt),
    EventType.StopAcquisitionPreview: CommandContract("acquisition", ("acquisition",), partial(_preview_request, action="stop"), _preview_receipt),
    EventType.GetAcquisitionPreview: CommandContract("acquisition", (), partial(_preview_request, action="get"), _preview_receipt),
    EventType.AttachProcessingInput: CommandContract("acquisition", ("processing",), _preparation_receipt, _attachment_receipt),
    EventType.DetachProcessingInput: CommandContract("acquisition", ("processing",), detach_input_request, _drain_receipt),
    EventType.StageProcessingConfiguration: CommandContract("processing", ("processing",), stage_configuration_request, _configuration_receipt),
    EventType.CancelProcessingConfiguration: CommandContract("processing", ("processing",), cancel_configuration_request, _configuration_receipt),
    EventType.PrepareSignalProcessing: CommandContract("processing", ("processing",), prepare_signal_request, _preparation_receipt),
    EventType.ResetProcessingInput: CommandContract("processing", ("processing",), reset_input_request, _reset_receipt),
    EventType.StartStimTrial: CommandContract("stimulation", ("stimulation",), stimulation_trial_request, _none),
    EventType.StopStimulation: CommandContract("stimulation", ("stimulation",), _none, _none),
    EventType.PauseStimulation: CommandContract("stimulation", ("stimulation",), _none,
        partial(_output_receipt, flags={"paused": True, "renderer_stopped": True}, counts=("cancelled_trials",))),
    EventType.ActuationCommand: CommandContract("actuation", ("actuation",), _actuation_request, None),
    EventType.SettleActuation: CommandContract("actuation", ("actuation",), _none,
        partial(_output_receipt, flags={"paused": True, "execution_complete": True, "outputs_settled": True}, counts=("pending_cancelled",))),
    EventType.ResumeActuation: CommandContract("actuation", ("actuation",), _none,
        partial(_output_receipt, flags={"resumed": True, "paused": False})),
}


def command_contract(event_type: EventType) -> CommandContract:
    if not isinstance(event_type, EventType) or event_type not in _CONTRACTS:
        raise ValueError(f"Not a v1 command: {event_type!r}")
    return _CONTRACTS[event_type]


def command_event(event: Event) -> Event:
    """Detach a validated payload before handing it to a queue or dispatcher."""
    contract = command_contract(event.type)
    request = CommandRequest.from_value(event.data) if contract.parse_result is not None else None
    if request is None:
        data = contract.parse_payload(event.data)
    else:
        data = CommandRequest(request.command_id, contract.parse_payload(request.payload))
    return Event(event.type, data)
