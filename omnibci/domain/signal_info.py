"""Immutable information about normalized signals, including their channels."""
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json


def fingerprint(value: dict) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def integer(value: object, name: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def name(value: object, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def record(value: object, field: str, allowed: set[str]) -> dict:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown {field} fields: {', '.join(sorted(map(str, unknown)))}")
    return dict(value)


def sequence(value: object, field: str) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array")
    return tuple(value)


@dataclass(frozen=True)
class ChannelDescriptor:
    index: int
    name: str
    signal_type: str = "unknown"
    unit: str | None = None
    name_provenance: str | None = None
    channel_id: str = ""

    def __post_init__(self) -> None:
        integer(self.index, "channel index")
        name(self.name, "channel name")
        name(self.signal_type, "signal_type")
        object.__setattr__(self, "signal_type", self.signal_type.strip().lower())
        if self.channel_id == "":
            object.__setattr__(self, "channel_id", f"channel-{self.index}")
        name(self.channel_id, "channel_id")
        if self.unit is not None:
            name(self.unit, "unit")
        if self.name_provenance not in (None, "generated"):
            raise ValueError("name_provenance must be generated or absent")

    def to_dict(self) -> dict:
        result = {"channel_id": self.channel_id, "index": self.index,
                  "name": self.name, "signal_type": self.signal_type}
        for field in ("unit", "name_provenance"):
            value = getattr(self, field)
            if value is not None:
                result[field] = value
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "ChannelDescriptor":
        data = record(value, "channel", set(cls.__dataclass_fields__))
        if not {"index", "name", "channel_id"} <= data.keys():
            raise ValueError("channel requires index, name, and channel_id")
        name(data["channel_id"], "channel_id")
        return cls(**data)


@dataclass(frozen=True)
class SignalInfo:
    source_id: str
    sample_rate: int
    channels: tuple[ChannelDescriptor, ...]
    trigger_row_index: int
    connection_id: str = ""
    native_event: str = "final-row"
    provenance: str = "protocol-defined"

    def __post_init__(self) -> None:
        name(self.source_id, "source_id")
        integer(self.sample_rate, "sample_rate", 1)
        channels = sequence(self.channels, "channels")
        if not channels or any(not isinstance(c, ChannelDescriptor) for c in channels):
            raise ValueError("channels must contain at least one ChannelDescriptor")
        if [c.index for c in channels] != list(range(len(channels))):
            raise ValueError("Acquisition channel indices must be ordered and contiguous from zero")
        if len({c.channel_id for c in channels}) != len(channels):
            raise ValueError("Acquisition channel IDs must be unique")
        integer(self.trigger_row_index, "trigger_row_index")
        if self.trigger_row_index != len(channels):
            raise ValueError("trigger_row_index must be the final row after all signal channels")
        if not isinstance(self.connection_id, str):
            raise ValueError("connection_id must be a string")
        name(self.native_event, "native_event")
        if (not isinstance(self.provenance, str)
                or self.provenance not in {"device-reported", "file-reported", "protocol-defined", "user-declared"}):
            raise ValueError("Invalid acquisition provenance")
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
        return fingerprint(data)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id, "sample_rate": self.sample_rate,
            "channels": [channel.to_dict() for channel in self.channels],
            "trigger_row_index": self.trigger_row_index, "connection_id": self.connection_id,
            "native_event": self.native_event, "provenance": self.provenance,
            "signal_channel_count": self.signal_channel_count, "output_row_count": self.output_row_count,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "SignalInfo":
        data = record(value, "signal info", set(cls.__dataclass_fields__) | {"signal_channel_count", "output_row_count"})
        if not {"source_id", "sample_rate", "channels", "trigger_row_index"} <= data.keys():
            raise ValueError("signal info requires source_id, sample_rate, channels, and trigger_row_index")
        signal_count = data.pop("signal_channel_count", None)
        row_count = data.pop("output_row_count", None)
        data["channels"] = tuple(ChannelDescriptor.from_dict(c) for c in sequence(data["channels"], "channels"))
        result = cls(**data)
        for field, actual, declared in (("signal_channel_count", result.signal_channel_count, signal_count),
                                       ("output_row_count", result.output_row_count, row_count)):
            if declared is not None:
                integer(declared, field, 1)
                if declared != actual:
                    raise ValueError(f"{field} does not match acquisition channels")
        return result
