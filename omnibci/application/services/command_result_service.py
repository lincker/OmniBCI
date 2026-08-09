"""Correlated command execution results for synchronous control use cases.

REST control handlers run in FastAPI's worker thread pool.  They may therefore
block for a short, bounded interval while the engine thread and a subprocess
execute a command.  ``CommandResultBroker`` owns that thread-safe rendezvous:

    caller -> Event(CommandRequest) -> worker
    caller <- Event(CommandResult)  <- worker

The EventBus callback never blocks; it only stores the result and signals the
waiting request thread.  Late results are ignored after the timeout removes the
waiter.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Protocol

from omnibci.application.events import (
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)
from omnibci.application.ports.engine_port import EnginePort
from omnibci.shared.events import Event, EventType

logger = logging.getLogger(__name__)


class CommandExecutor(Protocol):
    """Application port for commands that require a terminal worker result."""

    def request(
        self,
        event_type: EventType,
        payload=None,
        *,
        timeout_s: float | None = None,
    ) -> CommandResult: ...


class CommandRejectedError(RuntimeError):
    """The command reached its consumer but was not executed successfully."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(result.message or f"Command {result.command!r} was rejected")


class CommandExecutionError(RuntimeError):
    """The consumer accepted the command but execution failed internally."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        super().__init__(
            result.message or f"Command {result.command!r} failed during execution"
        )


class CommandTimeoutError(TimeoutError):
    """No terminal command result arrived before the configured deadline."""

    def __init__(self, command_id: str, command: str, timeout_s: float) -> None:
        self.command_id = command_id
        self.command = command
        self.timeout_s = timeout_s
        super().__init__(
            f"Timed out after {timeout_s:g}s waiting for command {command!r} "
            f"({command_id})"
        )


@dataclass
class _PendingCommand:
    done: threading.Event
    command: str
    result: CommandResult | None = None


class CommandResultBroker:
    """Dispatch commands with correlation and wait boundedly for their result."""

    def __init__(self, engine: EnginePort, timeout_s: float = 10.0) -> None:
        if timeout_s <= 0:
            raise ValueError("command timeout must be greater than zero")
        self._engine = engine
        self._timeout_s = float(timeout_s)
        self._pending: dict[str, _PendingCommand] = {}
        self._lock = threading.Lock()

    def request(
        self,
        event_type: EventType,
        payload=None,
        *,
        timeout_s: float | None = None,
    ) -> CommandResult:
        """Dispatch one command and return only after a successful worker result.

        Rejections preserve the consumer's error details, execution failures
        remain distinct from state rejection, and missing results time out.
        """
        wait_s = self._timeout_s if timeout_s is None else float(timeout_s)
        if wait_s <= 0:
            raise ValueError("command timeout must be greater than zero")

        command_id = str(uuid.uuid4())
        command_name = event_type.name
        pending = _PendingCommand(done=threading.Event(), command=command_name)
        with self._lock:
            self._pending[command_id] = pending

        try:
            self._engine.dispatch(Event(
                event_type,
                CommandRequest(command_id=command_id, payload=payload),
            ))
        except BaseException:
            with self._lock:
                self._pending.pop(command_id, None)
            raise

        completed = pending.done.wait(wait_s)
        with self._lock:
            self._pending.pop(command_id, None)
            result = pending.result

        # A result may race the timeout boundary: prefer an already-stored
        # terminal result even if Event.wait() returned False just before set().
        if not completed and result is None:
            raise CommandTimeoutError(command_id, command_name, wait_s)
        if result is None:  # defensive: done must imply a stored result
            raise CommandTimeoutError(command_id, command_name, wait_s)
        if not result.ok:
            if result.failure_kind is CommandFailureKind.EXECUTION_FAILED:
                raise CommandExecutionError(result)
            raise CommandRejectedError(result)
        return result

    def on_result(self, event: Event) -> None:
        """EventBus callback; safe to invoke from the engine thread."""
        result = CommandResult.from_value(event.data)
        if result is None:
            logger.error(
                "CommandResultBroker received invalid result payload: %r",
                event.data,
            )
            return
        with self._lock:
            pending = self._pending.get(result.command_id)
            if pending is None:
                logger.warning(
                    "Late or unknown command result ignored: id=%s command=%s",
                    result.command_id,
                    result.command,
                )
                return
            if pending.result is not None:
                logger.warning(
                    "Duplicate command result ignored: id=%s command=%s",
                    result.command_id,
                    result.command,
                )
                return
            if result.command != pending.command:
                logger.error(
                    "Command result name mismatch for id=%s: expected=%s actual=%s",
                    result.command_id,
                    pending.command,
                    result.command,
                )
                result = CommandResult(
                    command_id=result.command_id,
                    command=pending.command,
                    ok=False,
                    message=(
                        f"Command result mismatch: expected {pending.command!r}, "
                        f"received {result.command!r}"
                    ),
                    error_code="COMMAND_RESULT_MISMATCH",
                    failure_kind=CommandFailureKind.EXECUTION_FAILED,
                )
            pending.result = result
            pending.done.set()

    @property
    def pending_count(self) -> int:
        """Diagnostic/test view of currently waiting commands."""
        with self._lock:
            return len(self._pending)
