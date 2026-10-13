"""Processing mappings never execute retired montage configuration."""
from types import SimpleNamespace

import pytest

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


def test_legacy_global_setup_requires_reconfiguration_before_processing():
    with pytest.raises(ValueError, match="reconfigure"):
        processing_input({"SIGNAL.setup": {"source_id": "fixture", "outputs": ["O1"]}})


def test_legacy_profile_input_cannot_be_replaced_with_an_identity_mapping():
    binding = SimpleNamespace(processing_input_requires_review=True)
    with pytest.raises(ValueError, match="reconfigure"):
        materialize_processing_binding(binding, requires_channels=True)
