"""Host metadata projection; plugin signal layouts contain no connection identity."""
from copy import deepcopy

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


def metadata_receipt(value: dict) -> dict:
    """Validate and detach one source inspection result at the application boundary."""
    if (not isinstance(value, dict) or set(value) != {
            "source_id", "connection_id", "metadata", "descriptor", "descriptor_error"}
            or not isinstance(value["source_id"], str) or not value["source_id"]
            or not isinstance(value["connection_id"], str) or not value["connection_id"]
            or not isinstance(value["metadata"], str)
            or value["descriptor_error"] is not None and not isinstance(value["descriptor_error"], str)):
        raise ValueError("Source returned invalid acquisition metadata")
    if value["descriptor"] is not None:
        try:
            descriptor_layout(value["descriptor"])
        except (TypeError, ValueError) as exc:
            raise ValueError("Source returned invalid acquisition descriptor") from exc
        if any(value["descriptor"][key] != value[key] for key in ("source_id", "connection_id")):
            raise ValueError("Acquisition descriptor belongs to a different source connection")
    return deepcopy(value)
