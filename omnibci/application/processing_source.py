"""Resolve processing input and source ownership from the pinned runtime bindings."""
from copy import deepcopy

from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.application.source_expectations import validate_source_expectations


def materialize_processing_binding(binding):
    """Prepare historical all-source decoder input without changing its profile."""
    if binding.processing_input_requires_review:
        raise ValueError("Legacy processing input is unsupported; reconfigure the decoder channel mapping")
    value = binding.to_dict()
    value.pop("manual_montage", None)
    value.pop("acquisition_montage", None)
    if binding.slot == "processing.decoder" and binding.processing_input is None:
        value["processing_input"] = ProcessingInput().to_dict()
    return value


def binding_for(snapshot, slot):
    return next((item for item in snapshot.get("PLUGINS.bindings", ())
                 if item.get("slot") == slot), None)


def processing_input(snapshot):
    decoder = binding_for(snapshot, "processing.decoder")
    if decoder is not None and decoder.get("processing_input") is not None:
        return ProcessingInput.from_dict(decoder["processing_input"])
    if snapshot.get("SIGNAL.setup") is not None:
        raise ValueError("Legacy signal setup is unsupported; reconfigure the decoder channel mapping")
    return ProcessingInput() if decoder is not None else None


def requires_signal_preparation(snapshot):
    return processing_input(snapshot) is not None


def acquisition_binding_key(binding):
    """Saved profile revisions do not change ownership of the connected source."""
    if binding is None:
        return None
    return {key: deepcopy(binding.get(key)) for key in (
        "profile_id", "selection", "parameters",
    )}


def processing_binding_key(snapshot):
    value = processing_input(snapshot)
    return {"source_id": snapshot.get("ACQUISITION.signal_source"),
            "source": acquisition_binding_key(binding_for(snapshot, "acquisition.source")),
            "input": value.to_dict() if value is not None else None}


def validate_source_binding(snapshot, metadata):
    if metadata.source_id != snapshot.get("ACQUISITION.signal_source"):
        raise ValueError("Source metadata belongs to a different acquisition source")
    if metadata.provenance in {"device-reported", "file-reported"}:
        return
    expected_rate = snapshot.get("ACQUISITION.sample_rate")
    if expected_rate is not None and expected_rate != metadata.sample_rate:
        raise ValueError(f"Configured sample rate {expected_rate} differs from source rate {metadata.sample_rate}")
    expected_rows = snapshot.get("ACQUISITION.channel_number")
    if expected_rows is not None and expected_rows != metadata.output_row_count:
        raise ValueError("Source channel count differs from the selected acquisition profile")


def validate_plan_binding(snapshot, plan):
    validate_source_binding(snapshot, plan.source)
    validate_source_expectations(snapshot, plan.source)
    value = processing_input(snapshot)
    if value is None or plan.processing_input != value:
        raise ValueError("Signal plan does not match the selected processing input")
