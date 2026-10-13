"""Describe the adapter's normalized output independently of raw source fields."""
from math import isfinite

from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor, ChannelDescriptor


def acquisition_descriptor(raw: dict, *, source_id: str, connection_id: str) -> AcquisitionDescriptor:
    """Describe adapter output, whose final row contains trigger events."""
    rate = raw.get("sample_rate")
    if (isinstance(rate, bool) or not isinstance(rate, (int, float))
            or not isfinite(rate) or rate <= 0 or int(rate) != rate):
        raise ValueError("Source metadata requires a positive integer sample rate")
    count = raw.get("output_channel_count")
    if type(count) is not int or count < 2:
        raise ValueError("Source metadata requires at least one signal row and a final trigger row")
    signal_count = raw.get("channel_count")
    if signal_count is not None and (type(signal_count) is not int or signal_count != count - 1):
        raise ValueError("Source signal channel count must exclude the final trigger row")
    names = raw.get("channel_labels") or []
    if not isinstance(names, (list, tuple)):
        raise ValueError("Source channel labels must be a sequence")
    if names and len(names) != count - 1:
        raise ValueError(f"Source channel labels must describe exactly {count - 1} signal rows")
    types = raw.get("channel_types") or ["unknown"] * (count - 1)
    units = raw.get("channel_units") or [None] * (count - 1)
    channel_ids = raw.get("channel_ids")
    if channel_ids is None:
        channel_ids = [f"channel-{index}" for index in range(count - 1)]
    if any(not isinstance(values, (list, tuple)) or len(values) != count - 1
           for values in (types, units, channel_ids)):
        raise ValueError("Source channel types, units and IDs must match the signal row count")
    if any(not isinstance(value, str) or not value.strip() for value in channel_ids):
        raise ValueError("Source channel IDs must be nonempty strings")
    channels = []
    for index in range(count - 1):
        name = names[index] if names else None
        generated = name is None or isinstance(name, str) and not name.strip()
        channels.append(ChannelDescriptor(
            index=index, name=f"CH{index + 1}" if generated else name,
            channel_id=channel_ids[index],
            name_provenance="generated" if generated else None,
            signal_type=types[index], unit=units[index],
        ))
    return AcquisitionDescriptor(
        source_id=source_id, sample_rate=int(rate), connection_id=connection_id,
        channels=tuple(channels), trigger_row_index=count - 1,
        native_event=raw.get("native_event", "final-row"),
        provenance=raw.get("provenance", "protocol-defined"),
    )
