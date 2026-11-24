"""Resolve processing input and source ownership from the pinned runtime bindings."""
from copy import deepcopy

from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.application.source_metadata import descriptor_layout


def binding_for(snapshot, slot):
    if snapshot is None:
        return None
    bindings = snapshot.get("PLUGINS.bindings", ())
    return next((item for item in bindings if item.get("slot") == slot), None)


def binding_selection_id(binding) -> str | None:
    if not isinstance(binding, dict):
        return None
    selection = binding.get("selection")
    if isinstance(selection, dict):
        package_id = selection.get("package_id")
        contribution_id = selection.get("contribution_id")
        version = selection.get("version")
        if all(isinstance(part, str) and part for part in (package_id, contribution_id, version)):
            return f"{package_id}/{contribution_id}@{version}"
    return None


def source_id_for(snapshot) -> str | None:
    return binding_selection_id(binding_for(snapshot, "acquisition.source"))


def processing_input(snapshot):
    decoder = binding_for(snapshot, "processing.decoder")
    if decoder is not None and decoder.get("processing_input") is not None:
        value = decoder["processing_input"]
        return ProcessingInput.from_dict(value)
    if decoder is not None:
        raise ValueError("Decoder binding requires explicit processing_input")
    return None


def requires_signal_preparation(snapshot):
    return binding_for(snapshot, "processing.decoder") is not None


def acquisition_binding_key(binding):
    """Source ownership follows plugin values independently of saved profiles."""
    if binding is None:
        return None
    return {key: deepcopy(binding.get(key)) for key in (
        "selection", "parameters",
    )}


def processing_binding_key(snapshot):
    value = processing_input(snapshot)
    return {"source_id": source_id_for(snapshot),
            "source": acquisition_binding_key(binding_for(snapshot, "acquisition.source")),
            "input": value.to_dict() if value is not None else None}


def validate_source_binding(snapshot, metadata):
    info = descriptor_layout(metadata)
    if metadata["source_id"] != source_id_for(snapshot):
        raise ValueError("Source metadata belongs to a different acquisition source")
    if metadata["provenance"] in {"device-reported", "file-reported"}:
        return
    expected_rate = snapshot.get("ACQUISITION.sample_rate")
    if expected_rate is not None and expected_rate != info.sample_rate:
        raise ValueError(f"Configured sample rate {expected_rate} differs from source rate {info.sample_rate}")
    expected_rows = snapshot.get("ACQUISITION.channel_count")
    if expected_rows is not None and expected_rows != info.output_row_count:
        raise ValueError("Source channel count differs from the selected acquisition profile")


def validate_plan_binding(snapshot, plan):
    # Acquisition validates source identity; this owner validates the prepared layout/input.
    value = processing_input(snapshot)
    if value is None or plan.processing_input != value:
        raise ValueError("Signal plan does not match the selected processing input")
