"""
Acquisition domain ports.

  SignalSource   — abstraction over an EEG signal source (device or file);
  TriggerEmitter — abstraction over sending a trigger/event to an
                   external device.
"""
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
import json
from math import isfinite
from typing import Any, Literal, Protocol

from numpy import typing as npt

from omnibci.domain.signal_info import SignalInfo, name


@dataclass(frozen=True, kw_only=True)
class SourceObservation:
    native_text: str
    signal_info: SignalInfo | None
    native_event: str
    provenance: Literal["device-reported", "file-reported", "protocol-defined", "user-declared"]
    layout_error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.native_text, str):
            raise ValueError("Source native_text must be a string")
        name(self.native_event, "native_event")
        if self.provenance not in ("device-reported", "file-reported", "protocol-defined", "user-declared"):
            raise ValueError("Invalid acquisition provenance")
        if self.signal_info is None:
            name(self.layout_error, "layout_error")
            if len(self.layout_error) > 4096:
                raise ValueError("layout_error must not exceed 4096 characters")
        elif not isinstance(self.signal_info, SignalInfo) or self.layout_error is not None:
            raise ValueError("A valid SignalInfo requires layout_error=None")


@dataclass(frozen=True, kw_only=True)
class SourceCheckpoint:
    resource_id: str
    resource_revision: str
    state: Mapping[str, object]

    def __post_init__(self) -> None:
        name(self.resource_id, "resource_id")
        name(self.resource_revision, "resource_revision")
        if not isinstance(self.state, Mapping):
            raise ValueError("Checkpoint state must be a JSON object")
        state = dict(self.state)
        try:
            _validate_json(state)
            encoded = json.dumps(state, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 65536:
                raise ValueError("Checkpoint state must not exceed 65536 bytes")
            object.__setattr__(self, "state", json.loads(encoded))
        except (TypeError, RecursionError, OverflowError) as exc:
            raise ValueError("Checkpoint state must be bounded JSON") from exc


def _validate_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("Checkpoint state must contain only JSON values with string keys")


class SignalSource(ABC):
    """A source owns its transport; the host owns lifecycle state and read ordering."""

    @property
    @abstractmethod
    def trigger_mode(self) -> Literal["events", "level"]:
        """Fixed normalization mode, available immediately without I/O."""

    @property
    @abstractmethod
    def resume_mode(self) -> Literal["restart-only", "live-current", "checkpoint"]:
        """Fixed resume policy; live-current may lose samples during a pause."""

    @abstractmethod
    async def connect(self) -> None:
        """Connect successfully or raise; cleanup must tolerate partial setup."""

    @abstractmethod
    async def observe(self) -> SourceObservation:
        """Inspect a connected, stopped source with no pending read."""

    @abstractmethod
    async def start_stream(self) -> None:
        """Start delivery; a prior observation is not required."""

    @abstractmethod
    async def read_chunk(self) -> npt.NDArray[Any] | None:
        """Transfer a nonempty real matrix with a final event row, or None at EOF.

        Only one read may be active. Cancellation must settle before stopping,
        observing, checkpointing or disconnecting. Do not mutate returned data.
        """

    @abstractmethod
    async def stop_stream(self) -> None:
        """Stop after the read has settled; tolerate repeated cleanup."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Release resources, including after partial connection or prior cleanup."""

    def checkpoint(self) -> SourceCheckpoint | None:
        """Checkpoint sources override this to capture the next undelivered sample."""
        return None

    def restore_checkpoint(self, checkpoint: SourceCheckpoint) -> None:
        """Stage the next start while connected and stopped, with no pending read."""
        raise NotImplementedError("This signal source does not support checkpoints")


class TriggerOutput(Protocol):
    """Emission capability borrowed by a paradigm on its owner thread."""

    def send(self, event_id: int) -> None:
        """Write an event code; failures raise instead of returning false."""
        ...

    def clear(self) -> None:
        """Establish the device's clear baseline before the next event."""
        ...


class TriggerEmitter(ABC):
    @abstractmethod
    def setup(self) -> None:
        """Acquire driver resources on the host's rendering thread."""

    @abstractmethod
    def send(self, event_id: int) -> None:
        """Write an event code; successful return is not measured delivery."""

    @abstractmethod
    def clear(self) -> None:
        """Clear trigger output according to the device's timing contract."""

    @abstractmethod
    def teardown(self) -> None:
        """Release driver resources, including resources from partial setup."""
