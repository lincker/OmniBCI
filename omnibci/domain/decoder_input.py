"""Plugin-owned requirements for the final ordered decoder input channels."""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from omnibci.domain.montage import ChannelDescriptor


def _names(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be an array of channel names")
    if any(not isinstance(name, str) or not name.strip() for name in value):
        raise ValueError(f"{field} must contain nonempty channel names")
    names = tuple(name.strip() for name in value)
    if len(set(names)) != len(names):
        raise ValueError(f"{field} must contain unique channel names")
    return names


@dataclass(frozen=True)
class DecoderChannelRequirements:
    description: str | None = None
    signal_type: str | None = None
    min_count: int = 1
    required_names: tuple[str, ...] = ()
    default_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field in ("description", "signal_type"):
            value = getattr(self, field)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{field} must be a nonempty string when provided")
                object.__setattr__(self, field, value.strip())
        if type(self.min_count) is not int or self.min_count < 1:
            raise ValueError("min_count must be a positive integer")
        for field in ("required_names", "default_names"):
            object.__setattr__(self, field, _names(getattr(self, field), field))
        if self.default_names:
            if len(self.default_names) < self.min_count:
                raise ValueError("default_names must satisfy min_count")
            if not set(self.required_names).issubset(self.default_names):
                raise ValueError("default_names must include every required_names channel")

    @classmethod
    def from_dict(cls, value: dict) -> "DecoderChannelRequirements":
        if not isinstance(value, Mapping):
            raise ValueError("channel_requirements must be an object")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown channel_requirements fields: {', '.join(sorted(map(str, unknown)))}")
        return cls(**value)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "signal_type": self.signal_type,
            "min_count": self.min_count,
            "required_names": list(self.required_names),
            "default_names": list(self.default_names),
        }

    def validate_channels(self, channels: Sequence[ChannelDescriptor]) -> None:
        """Validate mapped outputs; callers must exclude trigger/reference-only rows."""
        if len(channels) < self.min_count:
            raise ValueError(
                f"Decoder requires at least {self.min_count} input channels; got {len(channels)}"
            )
        if self.signal_type is not None:
            mismatched = [channel.name for channel in channels if channel.signal_type != self.signal_type]
            if mismatched:
                raise ValueError(
                    f"Decoder requires {self.signal_type} input channels; incompatible channels: "
                    + ", ".join(mismatched)
                )
        names = {channel.name for channel in channels}
        missing = [name for name in self.required_names if name not in names]
        if missing:
            raise ValueError("Decoder input is missing required channels: " + ", ".join(missing))
