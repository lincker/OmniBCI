"""Select and name acquisition rows without changing sample values or events."""
from dataclasses import dataclass, field, replace

from omnibci.domain.signal_info import (
    SignalInfo,
    ChannelDescriptor,
    fingerprint,
    integer,
    name,
    record,
    sequence,
)
from omnibci.domain.signal import SignalChunk


@dataclass(frozen=True)
class ChannelBinding:
    name: str
    source_channel_id: str
    signal_type: str | None = None

    def __post_init__(self) -> None:
        name(self.name, "output channel name")
        name(self.source_channel_id, "source_channel_id")
        if self.signal_type is not None and (not isinstance(self.signal_type, str)
                or self.signal_type not in {"eeg", "eog", "ecg", "emg", "misc"}):
            raise ValueError("Declared signal_type must be eeg, eog, ecg, emg, or misc")

    def to_dict(self) -> dict:
        result = {"source_channel_id": self.source_channel_id, "name": self.name}
        if self.signal_type is not None:
            result["signal_type"] = self.signal_type
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "ChannelBinding":
        data = record(value, "channel binding", set(cls.__dataclass_fields__))
        if not {"name", "source_channel_id"} <= data.keys():
            raise ValueError("channel binding requires name and source_channel_id")
        return cls(**data)


@dataclass(frozen=True)
class ProcessingInput:
    schema_version: int = 2
    outputs: tuple[ChannelBinding, ...] = ()

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported processing input schema_version; replace legacy input with channel mapping")
        outputs = sequence(self.outputs, "outputs")
        if any(not isinstance(output, ChannelBinding) for output in outputs):
            raise ValueError("outputs must contain ChannelBinding records")
        if len({output.name for output in outputs}) != len(outputs):
            raise ValueError("Output names must be unique")
        if len({output.source_channel_id for output in outputs}) != len(outputs):
            raise ValueError("Acquisition channels cannot be mapped more than once")
        object.__setattr__(self, "outputs", outputs)

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "outputs": [output.to_dict() for output in self.outputs]}

    @classmethod
    def from_dict(cls, value: dict) -> "ProcessingInput":
        data = record(value, "processing input", set(cls.__dataclass_fields__))
        data["outputs"] = tuple(ChannelBinding.from_dict(o) for o in sequence(data.get("outputs", ()), "outputs"))
        return cls(**data)


@dataclass(frozen=True)
class ResolvedSignalPlan:
    source: SignalInfo
    output_channels: tuple[ChannelDescriptor, ...]
    source_indices: tuple[int, ...]
    processing_input: ProcessingInput
    config_revision: str = ""
    requires_data_mapping: bool = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, SignalInfo) or not isinstance(self.processing_input, ProcessingInput):
            raise ValueError("Plan requires an acquisition descriptor and processing input")
        outputs = sequence(self.output_channels, "output_channels")
        indices = sequence(self.source_indices, "source_indices")
        if not outputs or len(outputs) != len(indices):
            raise ValueError("Plan source indices must match nonempty outputs")
        if any(not isinstance(c, ChannelDescriptor) for c in outputs):
            raise ValueError("output_channels must contain ChannelDescriptor records")
        if [c.index for c in outputs] != list(range(len(outputs))):
            raise ValueError("Output channel indices must be ordered and contiguous")
        for index in indices:
            integer(index, "source index")
            if index >= self.source.signal_channel_count:
                raise ValueError("Source index is missing or is the trigger row")
        if len(set(indices)) != len(indices):
            raise ValueError("Source indices must be unique")
        if self.processing_input.outputs and len({c.name for c in outputs}) != len(outputs):
            raise ValueError("Mapped output names must be unique")
        if not isinstance(self.config_revision, str):
            raise ValueError("config_revision must be a string")
        object.__setattr__(self, "output_channels", outputs)
        object.__setattr__(self, "source_indices", indices)
        object.__setattr__(self, "requires_data_mapping", indices != tuple(range(self.source.signal_channel_count)))

    @property
    def fingerprint(self) -> str:
        data = self.to_dict(include_fingerprint=False)
        data["source"].pop("connection_id")
        data.pop("config_revision")
        return fingerprint(data)

    @property
    def is_identity(self) -> bool:
        return not self.requires_data_mapping and self.output_channels == self.source.channels

    def apply(self, chunk: SignalChunk) -> SignalChunk:
        rows = [*self.source_indices, self.source.trigger_row_index]
        return SignalChunk(timestamp=chunk.timestamp, sequence_id=chunk.sequence_id,
                           raw_data=chunk.raw_data[rows, :])

    def to_dict(self, *, include_fingerprint: bool = True) -> dict:
        result = {
            "source": self.source.to_dict(), "output_channels": [c.to_dict() for c in self.output_channels],
            "source_indices": list(self.source_indices), "processing_input": self.processing_input.to_dict(),
            "config_revision": self.config_revision,
        }
        if include_fingerprint:
            result["fingerprint"] = self.fingerprint
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "ResolvedSignalPlan":
        fields = {key for key, definition in cls.__dataclass_fields__.items() if definition.init}
        data = record(value, "signal plan", fields | {"fingerprint"})
        if not {"source", "output_channels", "source_indices", "processing_input"} <= data.keys():
            raise ValueError("signal plan requires source, output_channels, source_indices, and processing_input")
        expected_fingerprint = data.pop("fingerprint", None)
        data["source"] = SignalInfo.from_dict(data["source"])
        data["output_channels"] = tuple(ChannelDescriptor.from_dict(c) for c in sequence(data["output_channels"], "output_channels"))
        data["processing_input"] = ProcessingInput.from_dict(data["processing_input"])
        result = cls(**data)
        if expected_fingerprint is not None and expected_fingerprint != result.fingerprint:
            raise ValueError("Signal plan fingerprint does not match its contents")
        expected = compile_processing_plan(result.source, result.processing_input, result.config_revision)
        if result != expected:
            raise ValueError("Signal plan does not match its compiled acquisition descriptor and mapping")
        return result


def compile_processing_plan(
    descriptor: SignalInfo,
    processing_input: ProcessingInput,
    config_revision: str = "",
) -> ResolvedSignalPlan:
    if not isinstance(descriptor, SignalInfo) or not isinstance(processing_input, ProcessingInput):
        raise ValueError("Channel mapping requires an acquisition descriptor and processing input")
    by_id = {channel.channel_id: channel for channel in descriptor.channels}
    requested = processing_input.outputs or tuple(
        ChannelBinding(channel.name, channel.channel_id) for channel in descriptor.channels
    )
    channels = []
    indices = []
    for binding in requested:
        if binding.source_channel_id not in by_id:
            raise ValueError(f"Missing acquisition channel: {binding.source_channel_id}")
        channel = by_id[binding.source_channel_id]
        if channel.signal_type in {"trigger", "stim", "ground"}:
            raise ValueError(f"{channel.signal_type} channel cannot be mapped: {channel.channel_id}")
        if binding.signal_type is not None and channel.signal_type not in {"unknown", binding.signal_type}:
            raise ValueError(f"Declared type conflicts with acquisition channel {channel.channel_id}: {channel.signal_type}")
        indices.append(channel.index)
        channels.append(replace(channel, index=len(channels), name=binding.name,
                                signal_type=binding.signal_type or channel.signal_type,
                                name_provenance=channel.name_provenance if channel.name == binding.name else None))
    return ResolvedSignalPlan(descriptor, tuple(channels), tuple(indices), processing_input, config_revision)
