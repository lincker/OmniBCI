"""Saved profiles and accepted runtime content remain independent."""
import pickle
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginProfileValidationError,
    PluginSelection, ProfileBinding,
)
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput


def _legacy_montage():
    return {"source_id": "test-source", "sample_rate": 250, "trigger_row_index": 1,
            "channels": [{"index": 0, "name": "O1", "signal_type": "eeg", "unit": "V",
                          "position": [0.0, 0.01, 0.02], "coordinate_frame": "head",
                          "position_provenance": "file-reported"}], "provenance": "file-reported"}


def test_historical_acquisition_fields_round_trip_as_inert_raw_data():
    observed = _legacy_montage()
    profile = PluginProfile(str(uuid4()), "Source", "acquisition",
                            PluginSelection("acme.source", "main", "1.0.0"), {},
                            legacy={"acquisition_montage": observed, "manual_montage": observed})
    exported = profile.to_dict()
    assert exported["acquisition_montage"] == observed
    assert exported["manual_montage"] == observed
    assert not hasattr(profile, "acquisition_montage")
    assert not hasattr(profile, "manual_montage")
    assert PluginProfile.from_dict(exported) == profile
    observed["channels"][0]["name"] = "Input changed"
    exported["manual_montage"]["channels"][0]["name"] = "Export changed"
    assert profile.legacy["manual_montage"]["channels"][0]["name"] == "O1"


def test_historical_acquisition_fields_are_not_accepted_on_other_kinds():
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder",
                            PluginSelection("acme.decoder", "main", None), {})
    assert "acquisition_montage" not in profile.to_dict()
    assert "manual_montage" not in profile.to_dict()
    assert PluginProfile.from_dict(profile.to_dict()) == profile
    for field in ("acquisition_montage", "manual_montage"):
        with pytest.raises(PluginProfileValidationError, match="legacy"):
            replace(profile, legacy={field: _legacy_montage()})
    with pytest.raises(PluginProfileValidationError, match="legacy"):
        replace(_binding(), legacy={"manual_montage": _legacy_montage()})


def test_historical_fields_preserve_activation_revision_without_live_domain_objects():
    binding = _binding("acquisition.source", "acquisition", legacy={"manual_montage": _legacy_montage()})
    selected = ActivationSnapshot({}, (binding,))
    document = selected.to_dict()
    restored = ActivationSnapshot.from_dict(document)
    assert restored == selected
    assert restored.revision == document["revision"]
    assert restored.bindings[0].to_dict()["manual_montage"] == _legacy_montage()
    document["bindings"][0]["manual_montage"]["channels"][0]["name"] = "Changed"
    assert selected.bindings[0].legacy["manual_montage"]["channels"][0]["name"] == "O1"
    with pytest.raises(PluginProfileValidationError, match="revision"):
        ActivationSnapshot.from_dict(document)


def test_v1_processing_input_is_inert_and_requires_explicit_replacement():
    legacy_input = {"schema_version": 1, "outputs": ["O1"],
                    "reference": {"mode": "single", "channels": ["M1"]}, "excluded_channels": []}
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder",
                            PluginSelection("acme.decoder", "main", None), {})
    profile = PluginProfile.from_dict({**profile.to_dict(), "processing_input": legacy_input})
    binding = ProfileBinding.from_dict({**_binding().to_dict(), "processing_input": legacy_input})
    assert profile.processing_input is binding.processing_input is None
    assert profile.processing_input_requires_review and binding.processing_input_requires_review
    assert profile.to_dict()["processing_input"] == binding.to_dict()["processing_input"] == legacy_input
    accepted = ActivationSnapshot({}, (binding,))
    assert ActivationSnapshot.from_dict(accepted.to_dict()) == accepted
    old_revision = accepted.revision
    exported = accepted.to_dict()
    exported["bindings"][0]["processing_input"]["outputs"].append("O2")
    assert accepted.revision == old_revision
    assert accepted.bindings[0].legacy["processing_input"]["outputs"] == ["O1"]
    replacement = replace(profile, legacy={}, processing_input=ProcessingInput())
    assert not replacement.processing_input_requires_review
    assert replacement.to_dict()["processing_input"] == {"schema_version": 2, "outputs": []}


def test_processing_input_is_decoder_only_and_frozen_in_activation():
    channel_input = ProcessingInput(outputs=(ChannelBinding("O2", "channel-2", "eeg"), ChannelBinding("O1", "channel-0", "eeg")))
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder",
                            PluginSelection("acme.decoder", "main", None), {},
                            processing_input=channel_input)
    assert PluginProfile.from_dict(profile.to_dict()) == profile
    legacy = ActivationSnapshot({}, (_binding(),))
    assert "processing_input" not in legacy.bindings[0].to_dict()
    selected = ActivationSnapshot({}, (replace(_binding(), processing_input=channel_input),))
    assert ActivationSnapshot.from_dict(selected.to_dict()) == selected
    assert selected.revision != legacy.revision
    exported = selected.to_dict()
    exported["bindings"][0]["processing_input"]["outputs"].reverse()
    assert selected.bindings[0].processing_input == channel_input
    assert not selected.bindings[0].processing_input_requires_review
    with pytest.raises(PluginProfileValidationError, match="decoder"):
        replace(profile, kind="filter")
    with pytest.raises(PluginProfileValidationError, match="decoder"):
        replace(_binding("acquisition.source", "acquisition"), processing_input=channel_input)


def _binding(slot="processing.decoder", kind="decoder", **fields):
    return ProfileBinding(slot, kind, None, PluginSelection("acme.test", "main", "1.0.0"),
                          {"bands": [8, 12]}, **fields)


def test_profile_and_binding_copy_parameters_and_round_trip():
    parameters = {"bands": [8, 12]}
    profile = PluginProfile(str(uuid4()), " Example ", "decoder",
                            PluginSelection("acme.test", "main", None), parameters)
    binding = _binding()
    parameters["bands"].append(16)
    exported = profile.to_dict()
    exported["parameters"]["bands"].append(24)

    assert profile.name == "Example"
    assert profile.parameters == {"bands": [8, 12]}
    assert PluginProfile.from_dict(profile.to_dict()) == profile
    assert ProfileBinding.from_dict(binding.to_dict()) == binding
    assert "version" not in profile.selection.to_dict()
    assert "profile_id" not in binding.to_dict()


def test_snapshot_cannot_be_changed_by_mutating_inputs_or_getter_values():
    settings = {"extra": {"modes": ["test"]}}
    binding = _binding()
    snapshot = ActivationSnapshot(settings, [binding])
    revision = snapshot.revision
    settings["extra"]["modes"].append("input change")
    binding.parameters["bands"].append(16)
    snapshot.settings["extra"]["modes"].append("getter change")
    snapshot.bindings[0].parameters["bands"].append(24)
    document = snapshot.to_dict()
    document["bindings"][0]["parameters"]["bands"].append(32)

    assert snapshot.settings == {"extra": {"modes": ["test"]}}
    assert snapshot.bindings[0].parameters == {"bands": [8, 12]}
    assert snapshot.revision == revision
    assert ActivationSnapshot.from_dict(snapshot.to_dict()) == snapshot
    assert pickle.loads(pickle.dumps(snapshot)) == snapshot


def test_semantic_revision_ignores_profile_metadata_and_mapping_order():
    binding = _binding()
    original = ActivationSnapshot({"a": 1, "b": 2}, (binding,))
    equivalent = ActivationSnapshot({"b": 2, "a": 1}, (
        replace(binding, profile_id=str(uuid4()), profile_revision=8),
    ))
    assert original.revision == equivalent.revision
    changed = ActivationSnapshot(original.settings, (replace(binding, parameters={"bands": [8, 16]}),))
    assert original.revision != changed.revision


def test_filter_instance_order_is_preserved_and_affects_revision():
    first = _binding(f"processing.filter.{uuid4()}", "filter")
    second = _binding(f"processing.filter.{uuid4()}", "filter")
    original = ActivationSnapshot({}, (first, second))
    reordered = ActivationSnapshot({}, (second, first))

    assert original.bindings == (first, second)
    assert original.revision != reordered.revision
    with pytest.raises(PluginProfileValidationError, match="Duplicate"):
        ActivationSnapshot({}, (first, first))


@pytest.mark.parametrize(("slot", "kind"), [
    ("processing.decoder", "filter"), ("processing.filter.invalid", "filter"),
    ("acquisition.unknown", "acquisition"), ("../slot", "decoder"),
])
def test_slot_identity_and_kind_are_checked(slot, kind):
    with pytest.raises(PluginProfileValidationError):
        _binding(slot, kind)


@pytest.mark.parametrize("fields", [
    {"profile_id": "../outside"}, {"name": "\ninvalid"}, {"kind": "montage"},
    {"format_version": True}, {"configuration_version": 0}, {"revision": True},
    {"parameters": {"bad": None}}, {"parameters": {"bad": float("nan")}},
])
def test_invalid_profile_data_is_rejected(fields):
    values = {"profile_id": str(uuid4()), "name": "Example", "kind": "decoder",
              "selection": PluginSelection("acme.test", "main", "1.0.0"), "parameters": {}}
    with pytest.raises(PluginProfileValidationError):
        PluginProfile(**{**values, **fields})


def test_stored_snapshot_revision_and_format_are_verified():
    document = ActivationSnapshot({}, (_binding(),)).to_dict()
    for changed in ({**document, "revision": "0" * 64}, {**document, "format_version": 3},
                    {**document, "unexpected": True}):
        with pytest.raises(PluginProfileValidationError):
            ActivationSnapshot.from_dict(changed)
