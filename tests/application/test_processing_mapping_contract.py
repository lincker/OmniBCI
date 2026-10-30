"""Processing mappings never execute retired montage configuration."""
import pytest

from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
from omnibci.application.processing_source import materialize_processing_binding, processing_input
from omnibci.presentation.schemas.signal_setup import ProcessingInputModel


@pytest.mark.parametrize("field,value", [
    ("reference", {"mode": "original"}), ("excluded_channels", []),
    ("template", "standard_1020"), ("assignments", []),
])
def test_processing_request_rejects_retired_montage_fields(field, value):
    with pytest.raises(ValueError):
        ProcessingInputModel.model_validate({"schema_version": 2, field: value})


def test_processing_request_accepts_explicit_mapping_and_type_declaration():
    value = {"schema_version": 2, "outputs": [{"source_channel_id": "channel-0", "name": "Oz", "signal_type": "eeg"}]}
    assert ProcessingInputModel.model_validate(value).model_dump(exclude_none=True) == value
    assert ProcessingInputModel.model_validate(value).model_dump() == value


def test_processing_request_accepts_offline_source_rows_without_channel_ids():
    value = {"schema_version": 2, "outputs": [
        {"source_channel_index": 0, "name": "Oz", "signal_type": "eeg"},
        {"source_channel_index": 3, "name": "Eye", "signal_type": "eog"},
    ]}
    assert ProcessingInputModel.model_validate(value).model_dump(exclude_none=True) == value
    assert ProcessingInputModel.model_validate(value).model_dump() == value


@pytest.mark.parametrize("reference", [
    {}, {"source_channel_id": None}, {"source_channel_index": None},
    {"source_channel_id": "channel-0", "source_channel_index": 0},
    {"source_channel_index": -1}, {"source_channel_index": True},
    {"source_channel_index": 1.0}, {"source_channel_index": "1"},
])
def test_processing_request_rejects_invalid_or_ambiguous_source_rows(reference):
    with pytest.raises(ValueError):
        ProcessingInputModel.model_validate({"outputs": [{"name": "Input", **reference}]})


def test_retired_global_setup_does_not_configure_processing():
    assert processing_input({"SIGNAL.setup": {"source_id": "fixture", "outputs": ["O1"]}}) is None


def test_legacy_profile_input_cannot_be_replaced_with_an_identity_mapping():
    value = {"schema_version": 1, "outputs": ["O1"]}
    binding = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {},
                             legacy={"processing_input": value})
    projected = materialize_processing_binding(binding)
    assert projected["processing_input"] == value
    with pytest.raises(ValueError):
        processing_input({"PLUGINS.bindings": [projected]})
