"""Describe the adapter's normalized output independently of raw source fields."""
from math import isfinite

from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor


def signal_info(
    *,
    source_id: str,
    connection_id: str,
    sample_rate: int | float,
    signal_channel_count: int,
    channel_labels: list[str | None] | tuple[str | None, ...] | None = None,
    channel_types: list[str] | tuple[str, ...] | None = None,
    channel_units: list[str | None] | tuple[str | None, ...] | None = None,
    channel_ids: list[str] | tuple[str, ...] | None = None,
    native_event: str = "final-row",
    provenance: str = "protocol-defined",
) -> SignalInfo:
    """Describe adapter output, whose final row contains trigger events."""
    if (isinstance(sample_rate, bool) or not isinstance(sample_rate, (int, float))
            or not isfinite(sample_rate) or sample_rate <= 0 or int(sample_rate) != sample_rate):
        raise ValueError("Source metadata requires a positive integer sample rate")
    if type(signal_channel_count) is not int or signal_channel_count < 1:
        raise ValueError("Source metadata requires a positive integer signal channel count")
    names = channel_labels or []
    if not isinstance(names, (list, tuple)):
        raise ValueError("Source channel labels must be a sequence")
    if names and len(names) != signal_channel_count:
        raise ValueError(f"Source channel labels must describe exactly {signal_channel_count} signal rows")
    types = channel_types or ["unknown"] * signal_channel_count
    units = channel_units or [None] * signal_channel_count
    if channel_ids is None:
        channel_ids = [f"channel-{index}" for index in range(signal_channel_count)]
    if any(not isinstance(values, (list, tuple)) or len(values) != signal_channel_count
           for values in (types, units, channel_ids)):
        raise ValueError("Source channel types, units and IDs must match the signal row count")
    if any(not isinstance(value, str) or not value.strip() for value in channel_ids):
        raise ValueError("Source channel IDs must be nonempty strings")
    channels = []
    for index in range(signal_channel_count):
        name = names[index] if names else None
        generated = name is None or isinstance(name, str) and not name.strip()
        channels.append(ChannelDescriptor(
            index=index, name=f"CH{index + 1}" if generated else name,
            channel_id=channel_ids[index],
            name_provenance="generated" if generated else None,
            signal_type=types[index], unit=units[index],
        ))
    return SignalInfo(
        source_id=source_id, sample_rate=int(sample_rate), connection_id=connection_id,
        channels=tuple(channels), trigger_row_index=signal_channel_count,
        native_event=native_event, provenance=provenance,
    )


# Keep the unchanged Scan45 plugin compatible with the renamed builder.
acquisition_descriptor = signal_info
