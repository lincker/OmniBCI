"""Validation boundary for engine -> actuation command payloads.

Host correlation and trial origin are separate from schema-validated device
parameters. The v1 envelope has no optional identity or legacy flat form.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
import json
import math
import time
from uuid import uuid4

from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _text(value):
    return isinstance(value, str) and bool(value) and value == value.strip()


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _validate_origin(origin):
    if not isinstance(origin, dict) or set(origin) != {
        "generation", "trial_number", "processing_revision", "acquisition_revision", "forced", "produced_at",
    }:
        raise ValueError("origin has missing or unknown fields")
    if (not _text(origin["generation"]) or not _integer(origin["trial_number"])
            or origin["trial_number"] < 1 or type(origin["forced"]) is not bool):
        raise ValueError("origin requires generation, positive trial_number and boolean forced")
    if any(value is not None and not _text(value)
           for value in (origin["processing_revision"], origin["acquisition_revision"])):
        raise ValueError("origin revisions must be non-empty strings or null")
    stamp = origin["produced_at"]
    if not isinstance(stamp, str):
        raise ValueError("origin produced_at must include a timezone offset")
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("origin produced_at must include a timezone offset")


class InvalidActuationCommand(ValueError):
    """A rejected command, with only independently validated correlation."""
    def __init__(self, message, *, command_id=None, origin=None, target_id=None):
        super().__init__(message)
        self.command_id = command_id
        self.origin = deepcopy(origin)
        self.target_id = target_id

    @property
    def correlation(self):
        return {"command_id": self.command_id, "origin": deepcopy(self.origin), "target_id": self.target_id}


@dataclass(frozen=True, init=False)
class ActuationIntent:
    """Immutable, private host command; only parameters reach the Effector."""
    command_id: str
    issued_at: float
    _origin_json: str = field(repr=False)
    _parameters_json: str = field(repr=False)

    def __init__(self, command_id, issued_at, origin, parameters):
        target = parameters.get("target_id") if isinstance(parameters, dict) else None
        origin_error = None
        try:
            _validate_origin(origin)
        except (TypeError, ValueError) as exc:
            origin_error = str(exc)

        def invalid(message):
            raise InvalidActuationCommand(
                message, command_id=command_id if _text(command_id) else None,
                origin=origin if origin_error is None else None,
                target_id=target if _integer(target) else None,
            )

        if not _text(command_id):
            invalid("command_id must be a non-empty string")
        issued = _finite_number(issued_at)
        if issued is None:
            invalid("issued_at must be a finite number")
        if origin_error is not None:
            invalid(origin_error)
        if not isinstance(parameters, dict):
            invalid("parameters must be a dict")
        if not _integer(target):
            invalid("target_id must be a non-negative integer")
        event_id = parameters.get("event_id")
        if event_id is not None and not _integer(event_id):
            invalid("event_id must be None or a non-negative integer")
        confidence = parameters.get("confidence")
        if confidence is not None:
            numeric = _finite_number(confidence)
            if numeric is None or not 0 <= numeric <= 1:
                invalid("confidence must be None or a finite number in [0, 1]")
        try:
            encoded = json.dumps({key: value for key, value in parameters.items()
                                  if not (key in {"event_id", "confidence"} and value is None)},
                                 allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            invalid(f"parameters must contain JSON values: {exc}")
        object.__setattr__(self, "command_id", command_id)
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "_origin_json", json.dumps(origin, sort_keys=True))
        object.__setattr__(self, "_parameters_json", encoded)

    @property
    def origin(self):
        return json.loads(self._origin_json)

    @property
    def parameters(self):
        return json.loads(self._parameters_json)

    @property
    def target_id(self):
        return self.parameters["target_id"]

    @property
    def correlation(self):
        return {"command_id": self.command_id, "origin": self.origin, "target_id": self.target_id}

    def to_dict(self):
        return {"command_id": self.command_id, "issued_at": self.issued_at,
                "origin": self.origin, "parameters": self.parameters}


def validate_actuation_command(payload: object) -> ActuationIntent:
    if isinstance(payload, ActuationIntent):
        return payload
    if not isinstance(payload, dict):
        raise InvalidActuationCommand("payload must be a dict")
    if set(payload) != {"command_id", "issued_at", "origin", "parameters"}:
        raise InvalidActuationCommand("payload has missing or unknown envelope fields")
    return ActuationIntent(**payload)


def intent_from_trial(outcome):
    """Carry the producer's trial identity across the independent output session."""
    if not isinstance(outcome, TrialOutcome) or not isinstance(outcome.classification, Classification):
        raise ValueError("Actuation requires a canonical TrialOutcome")
    result = outcome.classification
    return ActuationIntent(
        command_id=uuid4().hex, issued_at=time.time(),
        origin={"generation": outcome.generation, "trial_number": outcome.trial_number,
                "processing_revision": outcome.processing_revision, "acquisition_revision": outcome.acquisition_revision,
                "forced": result.forced, "produced_at": result.produced_at.isoformat()},
        parameters={"target_id": result.target_id, "event_id": result.event_id, "confidence": result.confidence},
    )
