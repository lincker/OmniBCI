"""Immutable signal descriptions and fixed channel transformations.

Source indices describe adapter output after event normalization. Coordinates
are in meters; the final trigger row never participates in signal arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
from collections.abc import Mapping

import numpy as np

from omnibci.domain.signal import SignalChunk


_PROVENANCE = {
    "device-reported", "file-reported", "protocol-defined", "user-declared",
    "template-derived",
}
_VOLTAGE_SCALE = {"V": 1.0, "mV": 1e-3, "uV": 1e-6, "µV": 1e-6, "μV": 1e-6, "nV": 1e-9}


def _fingerprint(value: dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _integer(value: object, name: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _name(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _record(value: object, name: str, allowed: set[str]) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {name} fields: {', '.join(sorted(unknown))}")
    return dict(value)


def _sequence(value: object, name: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    return tuple(value)


@dataclass(frozen=True)
class ChannelDescriptor:
    index: int
    name: str
    signal_type: str = "eeg"
    unit: str | None = None
    reference: str | None = None
    position: tuple[float, float, float] | None = None
    coordinate_frame: str | None = None
    position_provenance: str | None = None
    derivation: tuple[str, str] | None = None
    name_provenance: str | None = None

    def __post_init__(self) -> None:
        _integer(self.index, "channel index")
        _name(self.name, "channel name")
        _name(self.signal_type, "signal_type")
        if self.name_provenance not in (None, "generated"):
            raise ValueError("name_provenance must be generated or absent")
        for key in ("unit", "reference", "coordinate_frame", "position_provenance"):
            value = getattr(self, key)
            if value is not None:
                _name(value, key)
        if self.position is not None:
            position = tuple(self.position)
            if len(position) != 3 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                for v in position
            ):
                raise ValueError("position must contain three finite coordinates in meters")
            if self.coordinate_frame is None or self.position_provenance is None:
                raise ValueError("position requires coordinate_frame and position_provenance")
            object.__setattr__(self, "position", position)
        elif self.coordinate_frame is not None or self.position_provenance is not None:
            raise ValueError("coordinate metadata requires a position")
        if self.derivation is not None:
            derivation = tuple(self.derivation)
            if len(derivation) != 2:
                raise ValueError("derivation requires positive and negative channel names")
            for name in derivation:
                _name(name, "derivation channel")
            if self.position is not None:
                raise ValueError("A bipolar derivation cannot have a physical electrode position")
            object.__setattr__(self, "derivation", derivation)

    def to_dict(self) -> dict:
        result = {"index": self.index, "name": self.name, "signal_type": self.signal_type}
        for key in ("unit", "reference", "position", "coordinate_frame", "position_provenance", "derivation", "name_provenance"):
            value = getattr(self, key)
            if value is not None:
                result[key] = list(value) if isinstance(value, tuple) else value
        return result

    @classmethod
    def from_dict(cls, value: dict) -> ChannelDescriptor:
        data = _record(value, "channel", set(cls.__dataclass_fields__))
        if "index" not in data or "name" not in data:
            raise ValueError("channel requires index and name")
        return cls(**data)


@dataclass(frozen=True)
class SourceMetadata:
    source_id: str
    sample_rate: int
    channels: tuple[ChannelDescriptor, ...]
    trigger_row_index: int
    connection_id: str = ""
    original_reference: str | None = None
    native_event: str = "final-row"
    provenance: str = "user-declared"

    def __post_init__(self) -> None:
        _name(self.source_id, "source_id")
        _integer(self.sample_rate, "sample_rate", 1)
        channels = _sequence(self.channels, "channels")
        if not channels or any(not isinstance(c, ChannelDescriptor) for c in channels):
            raise ValueError("channels must contain at least one ChannelDescriptor")
        if [c.index for c in channels] != list(range(len(channels))):
            raise ValueError("Source channel indices must be ordered and contiguous from zero")
        if len({c.name for c in channels}) != len(channels):
            raise ValueError("Source channel names must be unique")
        _integer(self.trigger_row_index, "trigger_row_index")
        if self.trigger_row_index != len(channels):
            raise ValueError("trigger_row_index must be the final row after all signal channels")
        if not isinstance(self.connection_id, str):
            raise ValueError("connection_id must be a string")
        if self.original_reference is not None:
            _name(self.original_reference, "original_reference")
        _name(self.native_event, "native_event")
        if not isinstance(self.provenance, str) or self.provenance not in _PROVENANCE:
            raise ValueError("Invalid metadata provenance")
        object.__setattr__(self, "channels", channels)

    @property
    def signal_channel_count(self) -> int:
        return len(self.channels)

    @property
    def output_row_count(self) -> int:
        return len(self.channels) + 1

    @property
    def fingerprint(self) -> str:
        data = self.to_dict()
        data.pop("connection_id")
        return _fingerprint(data)

    def to_dict(self) -> dict:
        result = {
            "source_id": self.source_id,
            "sample_rate": self.sample_rate,
            "channels": [channel.to_dict() for channel in self.channels],
            "trigger_row_index": self.trigger_row_index,
            "connection_id": self.connection_id,
            "native_event": self.native_event,
            "provenance": self.provenance,
            "signal_channel_count": self.signal_channel_count,
            "output_row_count": self.output_row_count,
        }
        if self.original_reference is not None:
            result["original_reference"] = self.original_reference
        return result

    @classmethod
    def from_dict(cls, value: dict) -> SourceMetadata:
        data = _record(value, "source metadata", set(cls.__dataclass_fields__) | {"signal_channel_count", "output_row_count"})
        for key in ("source_id", "sample_rate", "channels", "trigger_row_index"):
            if key not in data:
                raise ValueError(f"source metadata requires {key}")
        signal_count = data.pop("signal_channel_count", None)
        row_count = data.pop("output_row_count", None)
        data["channels"] = tuple(ChannelDescriptor.from_dict(c) for c in _sequence(data["channels"], "channels"))
        result = cls(**data)
        if signal_count is not None:
            _integer(signal_count, "signal_channel_count", 1)
        if row_count is not None:
            _integer(row_count, "output_row_count", 2)
        if signal_count is not None and signal_count != result.signal_channel_count:
            raise ValueError("signal_channel_count does not match channels")
        if row_count is not None and row_count != result.output_row_count:
            raise ValueError("output_row_count does not match channels plus trigger")
        return result


def merge_source_montage(observed: SourceMetadata, manual: SourceMetadata) -> SourceMetadata:
    """Fill absent source facts without changing known observations or row identity."""
    if observed.source_id != manual.source_id:
        raise ValueError("Manual montage source_id differs from the source")
    if (observed.sample_rate != manual.sample_rate
            or observed.trigger_row_index != manual.trigger_row_index):
        raise ValueError("Manual montage sample rate or output row count differs from the source")
    if (observed.original_reference and manual.original_reference
            and observed.original_reference != manual.original_reference):
        raise ValueError("Manual original reference differs from the source")
    channels = []
    for channel, declared in zip(observed.channels, manual.channels, strict=True):
        if (channel.name_provenance != "generated" and declared.name_provenance != "generated"
                and channel.name != declared.name):
            raise ValueError(f"Source row {channel.index} changed: expected {declared.name!r}, got {channel.name!r}")
        if channel.derivation is not None or declared.derivation is not None:
            raise ValueError("Acquisition montage cannot contain derived channels")
        values = {}
        for key in ("signal_type", "unit", "reference", "position"):
            current, supplied = getattr(channel, key), getattr(declared, key)
            missing = current is None or (key == "signal_type" and current == "unknown")
            supplied_missing = supplied is None or (key == "signal_type" and supplied == "unknown")
            if not missing and not supplied_missing and current != supplied:
                raise ValueError(f"Manual {key} conflicts with source channel {channel.name}")
            values[key] = supplied if missing else current
        if channel.position is not None:
            if declared.position is not None and declared.coordinate_frame != channel.coordinate_frame:
                raise ValueError(f"Manual coordinate frame conflicts with source channel {channel.name}")
            values.update(coordinate_frame=channel.coordinate_frame, position_provenance=channel.position_provenance)
        elif declared.position is not None:
            values.update(coordinate_frame=declared.coordinate_frame, position_provenance="user-declared")
        use_manual_name = channel.name_provenance == "generated" and declared.name_provenance != "generated"
        channels.append(replace(channel, name=declared.name if use_manual_name else channel.name,
                                name_provenance=None if use_manual_name else channel.name_provenance, **values))
    return replace(observed, channels=tuple(channels),
                   original_reference=observed.original_reference or manual.original_reference)


@dataclass(frozen=True)
class ChannelAssignment:
    source_index: int
    electrode: str
    expected_name: str
    unit: str | None = None
    signal_type: str | None = None

    def __post_init__(self) -> None:
        _integer(self.source_index, "source_index")
        _name(self.electrode, "electrode")
        _name(self.expected_name, "expected_name")
        for key in ("unit", "signal_type"):
            if getattr(self, key) is not None:
                _name(getattr(self, key), key)

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__dataclass_fields__ if getattr(self, key) is not None}


@dataclass(frozen=True)
class BipolarOutput:
    name: str
    positive: str
    negative: str

    def __post_init__(self) -> None:
        for key in self.__dataclass_fields__:
            _name(getattr(self, key), key)
        if self.positive == self.negative:
            raise ValueError("Bipolar positive and negative channels must differ")

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}


@dataclass(frozen=True)
class ReferenceSpec:
    mode: str = "original"
    channels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"original", "single", "average", "bipolar"}:
            raise ValueError("Unsupported reference mode")
        channels = _sequence(self.channels, "reference channels")
        for channel in channels:
            _name(channel, "reference channel")
        if len(set(channels)) != len(channels):
            raise ValueError("Reference channels must be unique")
        if self.mode == "single" and len(channels) != 1:
            raise ValueError("single reference requires exactly one channel")
        if self.mode == "average" and not channels:
            raise ValueError("average reference requires a nonempty explicit channel pool")
        if self.mode in {"original", "bipolar"} and channels:
            raise ValueError(f"{self.mode} reference does not accept reference channels")
        object.__setattr__(self, "channels", channels)

    def to_dict(self) -> dict:
        return {"mode": self.mode, "channels": list(self.channels)}


@dataclass(frozen=True)
class SignalSetup:
    source_id: str
    schema_version: int = 1
    assignments: tuple[ChannelAssignment, ...] = ()
    outputs: tuple[str | BipolarOutput, ...] = ()
    reference: ReferenceSpec = field(default_factory=ReferenceSpec)
    excluded_channels: tuple[str, ...] = ()
    original_reference: str | None = None
    template: str | None = None
    manual_metadata: SourceMetadata | None = None

    def __post_init__(self) -> None:
        _name(self.source_id, "source_id")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("Unsupported signal setup schema_version")
        for key in ("assignments", "outputs", "excluded_channels"):
            object.__setattr__(self, key, _sequence(getattr(self, key), key))
        if not isinstance(self.reference, ReferenceSpec):
            raise ValueError("reference must be a ReferenceSpec")
        if any(not isinstance(a, ChannelAssignment) for a in self.assignments):
            raise ValueError("assignments must contain ChannelAssignment values")
        if len({a.source_index for a in self.assignments}) != len(self.assignments):
            raise ValueError("Source rows cannot be assigned twice")
        if len({a.electrode for a in self.assignments}) != len(self.assignments):
            raise ValueError("Electrode assignments must be unique")
        for output in self.outputs:
            if not isinstance(output, BipolarOutput):
                _name(output, "output channel")
        names = [o.name if isinstance(o, BipolarOutput) else o for o in self.outputs]
        if len(names) != len(set(names)):
            raise ValueError("Output channel names must be unique")
        if self.reference.mode == "bipolar":
            if not self.outputs or any(not isinstance(o, BipolarOutput) for o in self.outputs):
                raise ValueError("bipolar reference requires explicit bipolar outputs")
        elif any(isinstance(o, BipolarOutput) for o in self.outputs):
            raise ValueError("Bipolar outputs require bipolar reference mode")
        for name in self.excluded_channels:
            _name(name, "excluded channel")
        if len(set(self.excluded_channels)) != len(self.excluded_channels):
            raise ValueError("Excluded channel names must be unique")
        for key in ("original_reference", "template"):
            if getattr(self, key) is not None:
                _name(getattr(self, key), key)
        if self.manual_metadata is not None:
            if not isinstance(self.manual_metadata, SourceMetadata):
                raise ValueError("manual_metadata must be SourceMetadata")
            if self.manual_metadata.source_id != self.source_id:
                raise ValueError("Manual metadata source_id does not match setup source_id")

    def to_dict(self) -> dict:
        result = {
            "schema_version": self.schema_version, "source_id": self.source_id,
            "assignments": [a.to_dict() for a in self.assignments],
            "outputs": [o.to_dict() if isinstance(o, BipolarOutput) else o for o in self.outputs],
            "reference": self.reference.to_dict(),
            "excluded_channels": list(self.excluded_channels),
        }
        for key in ("original_reference", "template"):
            if getattr(self, key) is not None:
                result[key] = getattr(self, key)
        if self.manual_metadata is not None:
            result["manual_metadata"] = self.manual_metadata.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: dict) -> SignalSetup:
        data = _record(value, "signal setup", set(cls.__dataclass_fields__))
        if "source_id" not in data:
            raise ValueError("signal setup requires source_id")
        assignments = []
        for value in _sequence(data.get("assignments", ()), "assignments"):
            assignment = _record(value, "assignment", set(ChannelAssignment.__dataclass_fields__))
            if not {"source_index", "electrode", "expected_name"} <= assignment.keys():
                raise ValueError("assignment requires source_index, electrode, and expected_name")
            assignments.append(ChannelAssignment(**assignment))
        data["assignments"] = tuple(assignments)
        outputs = []
        for output in _sequence(data.get("outputs", ()), "outputs"):
            if isinstance(output, Mapping):
                output = _record(output, "bipolar output", set(BipolarOutput.__dataclass_fields__))
                if set(output) != {"name", "positive", "negative"}:
                    raise ValueError("bipolar output requires name, positive, and negative")
                output = BipolarOutput(**output)
            outputs.append(output)
        data["outputs"] = tuple(outputs)
        data["reference"] = ReferenceSpec(**_record(data.get("reference", {}), "reference", {"mode", "channels"}))
        if "manual_metadata" in data:
            data["manual_metadata"] = SourceMetadata.from_dict(data["manual_metadata"])
        return cls(**data)


@dataclass(frozen=True)
class ResolvedSignalPlan:
    source: SourceMetadata
    output_channels: tuple[ChannelDescriptor, ...]
    coefficients: tuple[tuple[float, ...], ...]
    setup: SignalSetup | None = None
    config_revision: str = ""
    warnings: tuple[str, ...] = ()
    _operations: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        outputs = tuple(self.output_channels)
        coefficients = tuple(tuple(row) for row in self.coefficients)
        if not outputs or len(outputs) != len(coefficients):
            raise ValueError("Plan coefficients must match nonempty outputs")
        if [c.index for c in outputs] != list(range(len(outputs))):
            raise ValueError("Output channel indices must be ordered and contiguous")
        if len({c.name for c in outputs}) != len(outputs):
            raise ValueError("Output channel names must be unique")
        operations = []
        for row in coefficients:
            if len(row) != self.source.signal_channel_count or not all(math.isfinite(v) for v in row):
                raise ValueError("Plan coefficients must be finite and match source signal dimensions")
            indices = np.flatnonzero(row)
            values = np.asarray(row, dtype=float)[indices]
            indices.setflags(write=False)
            values.setflags(write=False)
            operations.append((indices, values))
        object.__setattr__(self, "output_channels", outputs)
        object.__setattr__(self, "coefficients", coefficients)
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "_operations", tuple(operations))

    @property
    def fingerprint(self) -> str:
        data = self.to_dict(include_fingerprint=False)
        data["source"].pop("connection_id")
        data.pop("config_revision")
        return _fingerprint(data)

    @property
    def is_identity(self) -> bool:
        return self.coefficients == tuple(
            tuple(float(i == j) for j in range(self.source.signal_channel_count))
            for i in range(self.source.signal_channel_count)
        ) and all(
            (output.name, output.signal_type, output.unit, output.reference)
            == (source.name, source.signal_type, source.unit, source.reference or self.source.original_reference)
            for output, source in zip(self.output_channels, self.source.channels, strict=True)
        )

    def apply(self, chunk: SignalChunk) -> SignalChunk:
        if chunk.raw_data.shape[0] != self.source.output_row_count:
            raise ValueError(
                f"Signal rows changed: expected {self.source.output_row_count}, got {chunk.raw_data.shape[0]}"
            )
        selection = all(len(indices) == 1 and values[0] == 1 for indices, values in self._operations)
        if selection:
            indices = [int(indices[0]) for indices, _ in self._operations]
            raw = chunk.raw_data[indices + [self.source.trigger_row_index], :].copy()
        else:
            raw = np.empty((len(self.output_channels) + 1, chunk.raw_data.shape[1]), dtype=np.result_type(chunk.raw_data.dtype, np.float64))
            for index, (indices, values) in enumerate(self._operations):
                # Evaluate only dependencies: 0 * NaN in an excluded row must
                # never contaminate an unrelated output.
                raw[index] = values @ chunk.data[indices, :]
            raw[-1] = chunk.triggers
        return SignalChunk(timestamp=chunk.timestamp, sequence_id=chunk.sequence_id, raw_data=raw)

    def to_dict(self, *, include_fingerprint: bool = True) -> dict:
        result = {
            "source": self.source.to_dict(),
            "output_channels": [c.to_dict() for c in self.output_channels],
            "coefficients": [list(row) for row in self.coefficients],
            "config_revision": self.config_revision,
            "warnings": list(self.warnings),
        }
        if self.setup is not None:
            result["setup"] = self.setup.to_dict()
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_dict(cls, value: dict) -> ResolvedSignalPlan:
        data = _record(value, "signal plan", {"source", "output_channels", "coefficients", "setup", "config_revision", "warnings", "fingerprint"})
        if not {"source", "output_channels", "coefficients"} <= data.keys():
            raise ValueError("signal plan requires source, output_channels, and coefficients")
        fingerprint = data.pop("fingerprint", None)
        data["source"] = SourceMetadata.from_dict(data["source"])
        data["output_channels"] = tuple(ChannelDescriptor.from_dict(c) for c in _sequence(data["output_channels"], "output_channels"))
        if "setup" in data:
            data["setup"] = SignalSetup.from_dict(data["setup"])
        result = cls(**data)
        if fingerprint is not None and fingerprint != result.fingerprint:
            raise ValueError("Signal plan fingerprint does not match its contents")
        expected = compile_signal_plan(result.source, result.setup, result.config_revision)
        if result.to_dict(include_fingerprint=False) != expected.to_dict(include_fingerprint=False):
            raise ValueError("Signal plan does not match its compiled source and setup")
        return result


def compile_signal_plan(
    metadata: SourceMetadata,
    setup: SignalSetup | None = None,
    config_revision: str = "",
) -> ResolvedSignalPlan:
    """Resolve names and compile fixed selection or rereferencing coefficients."""
    if setup is not None and setup.source_id != metadata.source_id:
        raise ValueError(f"Setup source {setup.source_id!r} does not match {metadata.source_id!r}")
    setup_reference = setup.original_reference if setup else None
    if setup_reference and metadata.original_reference and setup_reference != metadata.original_reference:
        raise ValueError("Declared original reference conflicts with source metadata")
    original_reference = metadata.original_reference or setup_reference
    channels = list(metadata.channels)
    if setup is not None:
        for assignment in setup.assignments:
            if assignment.source_index >= len(channels):
                raise ValueError(f"Source index {assignment.source_index} is missing or is the trigger row")
            channel = channels[assignment.source_index]
            if assignment.expected_name is not None and assignment.expected_name != channel.name:
                raise ValueError(f"Source row {channel.index} changed: expected {assignment.expected_name!r}, got {channel.name!r}")
            if assignment.unit and channel.unit and _VOLTAGE_SCALE.get(assignment.unit, assignment.unit) != _VOLTAGE_SCALE.get(channel.unit, channel.unit):
                raise ValueError(f"Declared unit conflicts with observed unit for {channel.name}")
            if assignment.signal_type and channel.signal_type not in {"unknown", assignment.signal_type}:
                raise ValueError(f"Declared type conflicts with observed type for {channel.name}")
            channels[channel.index] = replace(
                channel, name=assignment.electrode,
                unit=channel.unit or assignment.unit,
                signal_type=assignment.signal_type or channel.signal_type,
            )
    by_name = {c.name: c for c in channels}
    if len(by_name) != len(channels):
        raise ValueError("Resolved electrode names must be unique")
    exclusions = set(setup.excluded_channels) if setup else set()
    if exclusions - by_name.keys():
        raise ValueError(f"Unknown excluded channels: {', '.join(sorted(exclusions - by_name.keys()))}")
    reference = setup.reference if setup else ReferenceSpec()
    requested = setup.outputs if setup and setup.outputs else tuple(c.name for c in channels if c.name not in exclusions)
    if not requested:
        raise ValueError("At least one output channel is required")

    def resolve(name: str) -> ChannelDescriptor:
        if name not in by_name:
            raise ValueError(f"Missing signal dependency: {name}")
        if name in exclusions:
            raise ValueError(f"Excluded channel cannot be used: {name}")
        channel = by_name[name]
        if channel.signal_type.lower() in {"trigger", "stim", "ground"}:
            raise ValueError(f"{channel.signal_type} channel cannot be a signal dependency: {name}")
        return channel

    reference_channels = tuple(resolve(name) for name in reference.channels)
    if any(c.signal_type.lower() not in {"eeg", "reference"} for c in reference_channels):
        raise ValueError("Reference pool must contain explicitly typed EEG or reference signals")
    output_channels = []
    coefficients = []
    for output in requested:
        positive = resolve(output.positive if isinstance(output, BipolarOutput) else output)
        negatives = (resolve(output.negative),) if isinstance(output, BipolarOutput) else reference_channels
        row = [0.0] * metadata.signal_channel_count
        if reference.mode == "original":
            row[positive.index] = 1.0
            resolved = replace(positive, index=len(output_channels), reference=positive.reference or original_reference)
        else:
            dependencies = (positive, *negatives)
            references = {c.reference or original_reference for c in dependencies}
            if len(references) != 1 or next(iter(references)) in {None, "unknown", "n/a"}:
                raise ValueError("Rereferencing requires a known, compatible original reference for all dependencies")
            if any(c.unit not in _VOLTAGE_SCALE for c in dependencies):
                raise ValueError("Rereferencing requires established voltage units for all dependencies")
            if any(c.signal_type != positive.signal_type and c.signal_type != "reference" for c in negatives):
                raise ValueError("Rereferencing cannot mix incompatible signal types")
            row[positive.index] = _VOLTAGE_SCALE[positive.unit]
            for negative in negatives:
                row[negative.index] -= _VOLTAGE_SCALE[negative.unit] / len(negatives)
            reference_name = negatives[0].name if len(negatives) == 1 else "average(" + ",".join(c.name for c in negatives) + ")"
            resolved = replace(positive, index=len(output_channels), unit="V", reference=reference_name)
            if isinstance(output, BipolarOutput):
                resolved = replace(resolved, name=output.name, derivation=(output.positive, output.negative), position=None, coordinate_frame=None, position_provenance=None)
        output_channels.append(resolved)
        coefficients.append(tuple(row))
    warnings = []
    if any(c.unit is None for c in output_channels):
        warnings.append("Some output units are unknown; only original-reference selection is available for those signals.")
    if any(c.reference is None for c in output_channels):
        warnings.append("The original acquisition reference is unknown.")
    return ResolvedSignalPlan(
        source=metadata, output_channels=tuple(output_channels), coefficients=tuple(coefficients),
        setup=setup, config_revision=config_revision, warnings=tuple(warnings),
    )
