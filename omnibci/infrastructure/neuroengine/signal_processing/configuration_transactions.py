"""Stage configuration candidates and retain command receipts for one worker."""

import hashlib
import json

from omnibci.application.events import CommandFailureKind, CommandRequest, CommandResult
from omnibci.shared.events import EventType
from .pipeline_configuration import ConfiguredPipeline, build_pipeline_update


def configuration_receipt(command_id, status, *, generation, revision):
    return {"status": status, "command_id": command_id,
            "applied_revision": revision, "generation": generation}


class ConfigurationTransactions:
    """Own pending updates and replay receipts without installing runtime state."""

    def __init__(self):
        self._pending: tuple[str, ConfiguredPipeline] | None = None
        # Receipts live for this worker generation. A cancelled command cannot
        # become a new update if a delayed duplicate arrives later.
        self._results: dict[str, CommandResult] = {}
        self._fingerprints: dict[str, str] = {}

    @property
    def pending(self) -> tuple[str, ConfiguredPipeline] | None:
        return self._pending

    def stage(self, request: CommandRequest, *, generation, revision,
              current_snapshot, sample_rate, baseline_duration,
              signal_plan=None) -> tuple[bool, CommandResult | None]:
        """Return whether a new candidate was staged, and any immediate receipt."""
        fingerprint = None
        try:
            completed = self._results.get(request.command_id)
            previous = self._fingerprints.get(request.command_id)
            # Only an early cancellation has a receipt without an original
            # payload. It blocks every delayed stage using that command id.
            if completed is not None and previous is None:
                return False, completed
            fingerprint = hashlib.sha256(json.dumps(
                request.payload, sort_keys=True, allow_nan=False, separators=(",", ":"),
            ).encode()).hexdigest()
            if previous is not None and previous != fingerprint:
                return False, CommandResult(
                    command_id=request.command_id, command=EventType.StageProcessingConfiguration.name,
                    ok=False, message="A processing configuration command id cannot be reused with a different payload",
                    error_code="PROCESSING_CONFIGURATION_CONFLICT", failure_kind=CommandFailureKind.REJECTED,
                )
            if completed is not None:
                return False, completed
            self._fingerprints[request.command_id] = fingerprint
            if self._pending is not None:
                if self._pending[0] == request.command_id:
                    return False, None
                raise ValueError("Another processing configuration is waiting for a trial boundary")
            payload = request.payload
            if not isinstance(payload, dict) or set(payload) != {"settings_snapshot", "expected_revision", "generation"}:
                raise ValueError("Invalid processing configuration request")
            if (not isinstance(payload["generation"], str) or not payload["generation"]
                    or payload["generation"] != generation):
                raise ValueError("Signal Processing configuration generation mismatch")
            if (not isinstance(payload["expected_revision"], str)
                    or payload["expected_revision"] != revision):
                raise ValueError("Signal Processing configuration revision mismatch")
            candidate = build_pipeline_update(
                current_snapshot, payload["settings_snapshot"],
                sample_rate=sample_rate, baseline_duration=baseline_duration,
                signal_plan=signal_plan,
            )
        except Exception as exc:
            # Invalid JSON cannot reserve an id without a payload fingerprint.
            # A malformed duplicate also leaves the original command intact.
            result = CommandResult(
                command_id=request.command_id, command=EventType.StageProcessingConfiguration.name,
                ok=False, message=str(exc), error_code="PROCESSING_CONFIGURATION_REJECTED",
                failure_kind=CommandFailureKind.REJECTED,
            )
            if fingerprint is not None and request.command_id not in self._results and (
                self._pending is None or self._pending[0] != request.command_id
            ):
                self._results[request.command_id] = result
            return False, result
        self._pending = (request.command_id, candidate)
        return True, None

    def complete_applied(self, *, generation, revision) -> CommandResult | None:
        """Record success only after the owner has installed the pending candidate."""
        if self._pending is None:
            return None
        command_id, _ = self._pending
        self._pending = None
        return self._finish(command_id, ok=True, generation=generation, revision=revision)

    def cancel_pending(self, message, *, generation, revision) -> CommandResult | None:
        if self._pending is None:
            return None
        command_id, _ = self._pending
        self._pending = None
        return self._finish(command_id, ok=False, message=message,
                            error_code="PROCESSING_CONFIGURATION_CANCELLED",
                            generation=generation, revision=revision)

    def cancel(self, request: CommandRequest, *, generation, revision) -> list[CommandResult]:
        """Return a target's terminal receipt before acknowledging cancellation."""
        payload = request.payload
        if (not isinstance(payload, dict) or set(payload) != {"command_id", "generation"}
                or not isinstance(payload.get("command_id"), str) or not payload["command_id"]
                or not isinstance(payload.get("generation"), str) or not payload["generation"]
                or payload.get("generation") != generation):
            return [CommandResult(
                request.command_id, EventType.CancelProcessingConfiguration.name, False,
                "Invalid processing configuration cancellation", "INVALID_CONFIGURATION_CANCELLATION",
                CommandFailureKind.REJECTED,
            )]
        results = []
        target = payload["command_id"]
        if self._pending is not None and self._pending[0] == target:
            results.append(self.cancel_pending("Cancelled before the trial boundary",
                                               generation=generation, revision=revision))
        result = self._results.get(target)
        if result is None:
            # A cancellation received before a delayed stage command is a
            # tombstone: that command must never install later in this run.
            result = self._finish(target, ok=False, message="Cancelled before staging",
                                  error_code="PROCESSING_CONFIGURATION_CANCELLED",
                                  generation=generation, revision=revision)
            results.append(result)
        receipt = result.data or configuration_receipt(target, "cancelled", generation=generation, revision=revision)
        results.append(CommandResult(
            request.command_id, EventType.CancelProcessingConfiguration.name, True, data=receipt,
        ))
        return results

    def _finish(self, command_id, *, ok, generation, revision, message=None, error_code=None):
        result = CommandResult(
            command_id=command_id, command=EventType.StageProcessingConfiguration.name,
            ok=ok, message=message, error_code=error_code,
            failure_kind=None if ok else CommandFailureKind.REJECTED,
            data=configuration_receipt(command_id, "applied" if ok else "cancelled",
                                       generation=generation, revision=revision),
        )
        self._results[command_id] = result
        return result
