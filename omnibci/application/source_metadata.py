"""Host metadata projection; plugin signal layouts contain no connection identity."""
from omnibci.domain.ports.acquisition import SourceObservation
from omnibci.domain.signal_info import SignalInfo, fingerprint, integer, name, record


def source_descriptor(observation: SourceObservation, *, source_id: str, connection_id: str) -> dict | None:
    info = observation.signal_info
    if info is None:
        return None
    return {**info.to_dict(), "source_id": source_id, "connection_id": connection_id,
            "native_event": observation.native_event, "provenance": observation.provenance,
            "trigger_row_index": info.trigger_row_index, "signal_channel_count": info.signal_channel_count,
            "output_row_count": info.output_row_count}


def descriptor_layout(value: dict) -> SignalInfo:
    data = record(value, "source descriptor", {
        "source_id", "connection_id", "native_event", "provenance", "sample_rate", "channels",
        "trigger_row_index", "signal_channel_count", "output_row_count",
    })
    name(data.get("source_id"), "source_id")
    name(data.get("connection_id"), "connection_id")
    info = SignalInfo.from_dict({key: data.get(key) for key in ("sample_rate", "channels")})
    SourceObservation(native_text="", signal_info=info,
                      native_event=data.get("native_event"), provenance=data.get("provenance"))
    for field in ("trigger_row_index", "signal_channel_count", "output_row_count"):
        integer(data.get(field), field, 1)
        if data[field] != getattr(info, field):
            raise ValueError(f"{field} does not match acquisition channels")
    return info


def source_fingerprint(descriptor: dict) -> str:
    return fingerprint({key: value for key, value in descriptor.items() if key != "connection_id"})
