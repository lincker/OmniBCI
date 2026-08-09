"""Pickle-safe EventBus payload contracts owned by the application layer."""
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from omnibci.domain.classification import Classification


@dataclass(frozen=True)
class TrialOutcome:
    """TrialCompleted payload with session context and classification.

    ``trial_number`` is one-based; zero asks the read model to derive it.
    """
    session_id: str
    trial_number: int
    classification: Classification


@dataclass(frozen=True)
class SessionSummary:
    """SessionEnded payload with session identity and final trial count."""
    session_id: str
    total_trials: int


@dataclass(frozen=True)
class AcquisitionSnapshot:
    """Authoritative device state emitted by the acquisition worker."""
    connected: bool
    streaming: bool


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
        )
