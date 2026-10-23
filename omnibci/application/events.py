"""Pickle-safe EventBus payload contracts owned by the application layer."""
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from omnibci.domain.classification import Classification


@dataclass(frozen=True)
class TrialOutcome:
    """One decoded result, numbered within an engine generation."""
    generation: str
    trial_number: int
    classification: Classification
    config_revision: str | None = None


@dataclass(frozen=True)
class EpochProcessed:
    """One terminal epoch record for local inspection, separate from output commands."""

    generation: str
    epoch_number: int
    event_id: int | None
    config_revision: str | None
    sample_rate: int | None
    channel_count: int
    sample_count: int
    pre_trigger_samples: int
    started_at: datetime
    completed_at: datetime
    status: str
    reason: str
    classification: Classification | None = None
    trial_number: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class AcquisitionSnapshot:
    """Authoritative device state emitted by the acquisition worker."""
    connected: bool
    streaming: bool
    connection_id: str | None = None


@dataclass(frozen=True)
class StreamBarrier:
    """In-band marker written by acquisition after its producer has stopped."""

    command_id: str
    generation: str | None
    config_revision: str | None
    last_sequence: int


@dataclass(frozen=True)
class StreamDrainReceipt:
    """Signal Processing completion evidence; physical outputs are not acknowledged."""

    generation: str | None
    config_revision: str | None
    last_sequence: int
    completed_trials: int
    interrupted_epoch: bool
    outputs_settled: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_value(cls, value) -> "StreamDrainReceipt":
        if not isinstance(value, Mapping) or set(value) != {
            "generation", "config_revision", "last_sequence",
            "completed_trials", "interrupted_epoch", "outputs_settled",
        }:
            raise ValueError("Missing or invalid stream drain receipt")
        for field in ("generation", "config_revision"):
            if value[field] is not None and (
                not isinstance(value[field], str) or not value[field]
            ):
                raise ValueError(f"Invalid drain receipt {field}")
        if type(value["last_sequence"]) is not int or value["last_sequence"] < -1:
            raise ValueError("Invalid drain receipt sequence")
        if type(value["completed_trials"]) is not int or value["completed_trials"] < 0:
            raise ValueError("Invalid drain receipt trial count")
        if type(value["interrupted_epoch"]) is not bool or value["outputs_settled"] is not False:
            raise ValueError("Invalid drain receipt completion flags")
        return cls(**value)


@dataclass(frozen=True)
class CommandRequest:
    """Correlation envelope for a command that requires execution confirmation.

    ``payload`` remains command-specific.  The envelope itself is deliberately
    made only of pickle/JSON-friendly fields so the same contract can cross the
    local multiprocessing queues and the stimulation TCP channel.
    """

    command_id: str
    payload: Any = None

    @classmethod
    def from_value(cls, value: Any) -> "CommandRequest | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return None
        command_id = value.get("command_id")
        if not isinstance(command_id, str) or not command_id:
            return None
        return cls(command_id=command_id, payload=value.get("payload"))


class CommandFailureKind(str, Enum):
    """Why a command did not complete successfully."""

    REJECTED = "rejected"
    EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class CommandResult:
    """Terminal execution result returned by a command consumer."""

    command_id: str
    command: str
    ok: bool
    message: str | None = None
    error_code: str | None = None
    failure_kind: CommandFailureKind | None = None
    data: dict[str, Any] | None = None

    @classmethod
    def from_value(cls, value: Any) -> "CommandResult | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return None
        command_id = value.get("command_id")
        command = value.get("command")
        ok = value.get("ok")
        if (
            not isinstance(command_id, str)
            or not command_id
            or not isinstance(command, str)
            or not command
            or not isinstance(ok, bool)
        ):
            return None
        message = value.get("message")
        error_code = value.get("error_code")
        raw_failure_kind = value.get("failure_kind")
        data = value.get("data")
        if data is not None and not isinstance(data, Mapping):
            return None
        try:
            failure_kind = (
                CommandFailureKind(raw_failure_kind)
                if raw_failure_kind is not None
                else None
            )
        except ValueError:
            return None
        return cls(
            command_id=command_id,
            command=command,
            ok=ok,
            message=message if isinstance(message, str) else None,
            error_code=error_code if isinstance(error_code, str) else None,
            failure_kind=failure_kind,
            data=dict(data) if data is not None else None,
        )
