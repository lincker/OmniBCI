"""Immutable information about normalized signals, including their channels."""
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    channel_id: str = field(kw_only=True)
    signal_type: str = "unknown"
    unit: str | None = None
    name_provenance: str | None = None

    def __post_init__(self) -> None:
        integer(self.index, "channel index")
        name(self.name, "channel name")
        name(self.signal_type, "signal_type")
        object.__setattr__(self, "signal_type", self.signal_type.strip().lower())
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
    sample_rate: int
    channels: tuple[ChannelDescriptor, ...]

    def __post_init__(self) -> None:
        integer(self.sample_rate, "sample_rate", 1)
        channels = sequence(self.channels, "channels")
        if not channels or any(not isinstance(c, ChannelDescriptor) for c in channels):
            raise ValueError("channels must contain at least one ChannelDescriptor")
        if [c.index for c in channels] != list(range(len(channels))):
            raise ValueError("Acquisition channel indices must be ordered and contiguous from zero")
        if len({c.channel_id for c in channels}) != len(channels):
            raise ValueError("Acquisition channel IDs must be unique")
        object.__setattr__(self, "channels", channels)

    @property
    def signal_channel_count(self) -> int:
        return len(self.channels)

    @property
    def trigger_row_index(self) -> int:
        return len(self.channels)

    @property
    def output_row_count(self) -> int:
        return len(self.channels) + 1

    def to_dict(self) -> dict:
        return {"sample_rate": self.sample_rate,
                "channels": [channel.to_dict() for channel in self.channels]}

    @classmethod
    def from_dict(cls, value: dict) -> "SignalInfo":
        data = record(value, "signal info", {"sample_rate", "channels"})
        if set(data) != {"sample_rate", "channels"}:
            raise ValueError("signal info requires sample_rate and channels")
        return cls(sample_rate=data["sample_rate"], channels=tuple(
            ChannelDescriptor.from_dict(c) for c in sequence(data["channels"], "channels")))
