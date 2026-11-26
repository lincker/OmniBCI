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
from omnibci.application.runtime_admission import admitted
from omnibci.application.command_contracts import CommandContract, command_contract
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Protocol

from omnibci.application.events import (
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)
from omnibci.application.ports.ipc_port import CommandDispatcherPort
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


class CommandDeliveryError(RuntimeError):
    """Local delivery failed; no terminal execution result was received."""

    def __init__(self, command_id: str, command: str, reason: str, *, execution_unknown: bool):
        self.command_id = command_id
        self.command = command
        self.reason = reason
        self.execution_unknown = execution_unknown
        outcome = "execution outcome is unknown" if execution_unknown else "command was not sent"
        super().__init__(f"{reason}; {command}: {outcome}")


@dataclass
class _PendingCommand:
    done: threading.Event
    command: str
    contract: CommandContract
    peer: str | None = None
    result: CommandResult | None = None
    delivery_error: CommandDeliveryError | None = None


class CommandResultBroker:
    """Dispatch commands with correlation and wait boundedly for their result."""

    def __init__(self, dispatcher: CommandDispatcherPort, timeout_s: float = 10.0, *, admission=None, dispatcher_name="Engine") -> None:
        if timeout_s <= 0:
            raise ValueError("command timeout must be greater than zero")
        self._dispatcher_name = dispatcher_name
        self._dispatcher = dispatcher
        self._admission = admission
        self._timeout_s = float(timeout_s)
        self._pending: dict[str, _PendingCommand] = {}
        self._lock = threading.Lock()

    @admitted(scopes=lambda self, event_type, *args, **kwargs: command_contract(event_type).scopes)
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

        contract = command_contract(event_type)
        if contract.parse_result is None:
            raise ValueError("This command uses its own result event")
        payload = contract.parse_payload(payload)
        command_id = str(uuid.uuid4())
        command_name = event_type.name
        scopes = contract.scopes
        peer = next((scope for scope in scopes if scope in {"stimulation", "actuation"}), None)
        pending = _PendingCommand(done=threading.Event(), command=command_name, contract=contract, peer=peer)
        with self._lock:
            self._pending[command_id] = pending

        try:
            self._dispatcher.dispatch(Event(
                event_type,
                CommandRequest(command_id=command_id, payload=payload),
            ))
        except BaseException:
            with self._lock:
                self._pending.pop(command_id, None)
            raise

        deadline = time.monotonic() + wait_s
        dispatcher_stopped = False
        module_stopped = False
        while not pending.done.wait(min(max(deadline - time.monotonic(), 0), 0.1)):
            if not self._dispatcher.is_ready:
                dispatcher_stopped = True
                break
            if not self._dispatcher.is_command_ready(event_type):
                module_stopped = True
                break
            if time.monotonic() >= deadline:
                break
        with self._lock:
            self._pending.pop(command_id, None)
            result = pending.result
            delivery_error = pending.delivery_error

        # A result may race the timeout boundary: prefer an already-stored
        # terminal result even when a deadline or worker exit was just observed.
        if result is None and delivery_error is not None:
            raise delivery_error
        if result is None and dispatcher_stopped:
            raise CommandExecutionError(CommandResult(
                command_id=command_id, command=command_name, ok=False,
                message=f"{self._dispatcher_name} stopped before confirming command execution",
                error_code=f"{self._dispatcher_name.upper()}_STOPPED", failure_kind=CommandFailureKind.EXECUTION_FAILED,
            ))
        if result is None and module_stopped:
            raise CommandExecutionError(CommandResult(
                command_id=command_id, command=command_name, ok=False,
                message="Module stopped before confirming command execution",
                error_code="MODULE_STOPPED", failure_kind=CommandFailureKind.EXECUTION_FAILED,
            ))
        if result is None:
            raise CommandTimeoutError(command_id, command_name, wait_s)
        if not result.ok:
            if result.failure_kind is CommandFailureKind.EXECUTION_FAILED:
                raise CommandExecutionError(result)
            raise CommandRejectedError(result)
        return result

    def on_peer_delivery_fault(self, event: Event) -> None:
        """Release this peer's waiters without fabricating execution receipts."""
        fault = event.data
        if not isinstance(fault, dict) or fault.get("peer") not in {"stimulation", "actuation"}:
            return
        unsent = fault.get("unsent_command_ids", ())
        with self._lock:
            for command_id, pending in self._pending.items():
                if pending.peer != fault["peer"] or pending.result is not None or pending.delivery_error is not None:
                    continue
                pending.delivery_error = CommandDeliveryError(
                    command_id, pending.command, fault["reason"],
                    execution_unknown=command_id not in unsent,
                )
                pending.done.set()

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
            elif result.ok:
                try:
                    result = replace(result, data=pending.contract.parse_result(result.data))
                except (TypeError, ValueError) as exc:
                    result = CommandResult(
                        command_id=result.command_id, command=pending.command, ok=False,
                        message=f"Invalid command result: {exc}", error_code="INVALID_COMMAND_RESULT",
                        failure_kind=CommandFailureKind.EXECUTION_FAILED,
                    )
            pending.result = result
            pending.done.set()

    @property
    def pending_count(self) -> int:
        """Diagnostic/test view of currently waiting commands."""
        with self._lock:
            return len(self._pending)
