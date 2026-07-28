"""Validation boundary for engine -> actuation command payloads.

The IPC decoder deliberately validates only the generic event envelope.  This
module owns the stricter, actuation-specific payload contract that must be
satisfied before an Effector is allowed to see a command.

Unknown keys are preserved for device-specific extensions.  The standard
fields are validated without coercion so malformed wire values (for example
``"3"`` or ``True`` for an integer field) cannot silently become device
commands.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any


_MISSING = object()


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_number(value: object) -> float | None:
    """Return *value* as a finite float, or None when it is not wire-numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class InvalidActuationCommand(ValueError):
    """A command payload failed validation.

    ``target_id`` and ``seq`` contain only independently validated correlation
    values.  They are safe to echo in an ``INVALID_COMMAND`` fault even when a
    different field made the payload invalid.
    """

    def __init__(self, message: str, *, target_id: int | None = None,
                 seq: int | None = None) -> None:
        super().__init__(message)
        self.target_id = target_id
        self.seq = seq


@dataclass(frozen=True)
class ValidatedActuationCommand:
    """Validated command plus correlation metadata used by the runtime."""

    payload: dict[str, Any]
    target_id: int
    seq: int | None
    issued_at: float | None


def validate_actuation_command(payload: object) -> ValidatedActuationCommand:
    """Validate one ``ActuationCommand.data`` value without coercing it.

    Required:
      - ``target_id``: non-negative integer (``bool`` is rejected).

    Optional correlation/freshness fields:
      - ``seq``: when present, a non-negative integer;
      - ``issued_at``: when present, a finite JSON number.

    Optional decoded-result fields:
      - ``event_id``: None or a non-negative integer;
      - ``confidence``: None or a finite number in [0, 1].

    Older senders may omit every optional field.  Unknown fields are retained
    because concrete Effectors may define device-specific command extensions.
    """
    if not isinstance(payload, dict):
        raise InvalidActuationCommand("payload must be a dict")

    raw_target = payload.get("target_id", _MISSING)
    raw_seq = payload.get("seq", _MISSING)
    correlation_target = raw_target if _is_non_negative_int(raw_target) else None
    correlation_seq = raw_seq if _is_non_negative_int(raw_seq) else None

    def invalid(message: str) -> None:
        raise InvalidActuationCommand(
            message,
            target_id=correlation_target,
            seq=correlation_seq,
        )

    if not _is_non_negative_int(raw_target):
        invalid("target_id must be a non-negative integer")
    target_id = raw_target

    if raw_seq is _MISSING:
        seq = None
    elif not _is_non_negative_int(raw_seq):
        invalid("seq must be a non-negative integer when present")
    else:
        seq = raw_seq

    raw_issued_at = payload.get("issued_at", _MISSING)
    if raw_issued_at is _MISSING:
        issued_at = None
    else:
        issued_at = _finite_number(raw_issued_at)
        if issued_at is None:
            invalid("issued_at must be a finite number when present")

    raw_event_id = payload.get("event_id", _MISSING)
    if (raw_event_id is not _MISSING and raw_event_id is not None
            and not _is_non_negative_int(raw_event_id)):
        invalid("event_id must be None or a non-negative integer")

    raw_confidence = payload.get("confidence", _MISSING)
    if raw_confidence is not _MISSING and raw_confidence is not None:
        confidence = _finite_number(raw_confidence)
        if confidence is None or not 0.0 <= confidence <= 1.0:
            invalid("confidence must be None or a finite number in [0, 1]")

    # Snapshot at the trust boundary.  Device extensions may contain nested
    # JSON objects, so a shallow dict copy would still let callers mutate a
    # command after it had passed validation and entered the mailbox.
    return ValidatedActuationCommand(
        payload=deepcopy(payload),
        target_id=target_id,
        seq=seq,
        issued_at=issued_at,
    )
