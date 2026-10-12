"""Resolve processing input and source ownership from the pinned runtime bindings."""
from copy import deepcopy

from omnibci.domain.montage import ProcessingInput, SignalSetup


def materialize_processing_binding(binding, *, requires_channels, legacy_setup):
    """Prepare historical all-source decoder input without changing its profile."""
    value = binding.to_dict()
    if (binding.slot == "processing.decoder" and requires_channels
            and binding.processing_input is None and legacy_setup is None):
        value["processing_input"] = ProcessingInput().to_dict()
    return value


def binding_for(snapshot, slot):
    return next((item for item in snapshot.get("PLUGINS.bindings", ())
                 if item.get("slot") == slot), None)


def processing_input(snapshot):
    decoder = binding_for(snapshot, "processing.decoder")
    if decoder is not None and decoder.get("processing_input") is not None:
        return ProcessingInput.from_dict(decoder["processing_input"])
    legacy = snapshot.get("SIGNAL.setup")
    return SignalSetup.from_dict(legacy) if legacy is not None else None


def requires_signal_preparation(snapshot):
    decoder = binding_for(snapshot, "processing.decoder")
    return (decoder is not None and decoder.get("processing_input") is not None
            or snapshot.get("SIGNAL.setup") is not None)


def acquisition_binding_key(binding):
    """Observation-only profile revisions do not change ownership of source data."""
    if binding is None:
        return None
    return {key: deepcopy(binding.get(key)) for key in (
        "profile_id", "selection", "parameters", "manual_montage",
    )}


def processing_binding_key(snapshot):
    value = processing_input(snapshot)
    return {"source_id": snapshot.get("ACQUISITION.signal_source"),
            "source": acquisition_binding_key(binding_for(snapshot, "acquisition.source")),
            "input": value.to_dict() if value is not None else None}


def validate_source_binding(snapshot, metadata):
    if metadata.source_id != snapshot.get("ACQUISITION.signal_source"):
        raise ValueError("Source metadata belongs to a different acquisition source")
    expected_rate = snapshot.get("ACQUISITION.sample_rate")
    if expected_rate is not None and expected_rate != metadata.sample_rate:
        raise ValueError(f"Configured sample rate {expected_rate} differs from source rate {metadata.sample_rate}")
    expected_rows = snapshot.get("ACQUISITION.channel_number")
    if expected_rows is not None and expected_rows != metadata.output_row_count:
        raise ValueError("Source channel count differs from the selected acquisition profile")


def validate_plan_binding(snapshot, plan):
    validate_source_binding(snapshot, plan.source)
    value = processing_input(snapshot)
    if isinstance(value, ProcessingInput):
        matches = plan.processing_input == value and plan.setup is None
    else:
        matches = plan.setup == value and plan.processing_input is None
    if not matches:
        raise ValueError("Signal plan does not match the selected processing input")
