"""Saved profiles and accepted runtime content remain independent."""
import pickle
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginProfileValidationError,
    PluginSelection, ProfileBinding,
)
from omnibci.domain.montage import ChannelDescriptor, SourceMetadata


def _montage():
    return SourceMetadata("test-source", 250, (
        ChannelDescriptor(0, "O1", unit="V", position=(0.0, 0.01, 0.02),
                          coordinate_frame="head", position_provenance="file-reported"),
    ), 1, connection_id="live-connection", provenance="file-reported")


def test_profile_montages_round_trip_without_persisting_connection_identity():
    observed = _montage()
    profile = PluginProfile(str(uuid4()), "Source", "acquisition",
                            PluginSelection("acme.source", "main", "1.0.0"), {},
                            acquisition_montage=observed, manual_montage=observed)
    exported = profile.to_dict()
    assert "connection_id" not in exported["acquisition_montage"]
    assert "connection_id" not in exported["manual_montage"]
    assert profile.acquisition_montage.connection_id == ""
    assert profile.acquisition_montage.channels[0].position_provenance == "file-reported"
    assert profile.manual_montage.provenance == "user-declared"
    assert profile.manual_montage.channels[0].position_provenance == "user-declared"
    assert observed.connection_id == "live-connection"
    assert PluginProfile.from_dict(exported) == profile
    exported["manual_montage"]["channels"][0]["name"] = "Changed"
    assert profile.manual_montage.channels[0].name == "O1"


def test_montage_fields_are_acquisition_only_and_omitted_for_legacy_documents():
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder",
                            PluginSelection("acme.decoder", "main", None), {})
    assert "acquisition_montage" not in profile.to_dict()
    assert "manual_montage" not in profile.to_dict()
    assert PluginProfile.from_dict(profile.to_dict()) == profile
    for field in ("acquisition_montage", "manual_montage"):
        with pytest.raises(PluginProfileValidationError, match="acquisition"):
            replace(profile, **{field: _montage()})
    with pytest.raises(PluginProfileValidationError, match="acquisition"):
        replace(_binding(), manual_montage=_montage())


def test_manual_montage_is_frozen_in_binding_and_affects_activation_revision():
    binding = _binding("acquisition.source", "acquisition")
    legacy = ActivationSnapshot({}, (binding,))
    restored = ActivationSnapshot.from_dict(legacy.to_dict())
    assert restored.revision == legacy.revision
    assert "manual_montage" not in restored.bindings[0].to_dict()
    manual = replace(binding, manual_montage=_montage())
    selected = ActivationSnapshot({}, (manual,))
    assert selected.revision != legacy.revision
    assert ActivationSnapshot.from_dict(selected.to_dict()) == selected
    assert selected.bindings[0].manual_montage.connection_id == ""
    assert "acquisition_montage" not in selected.bindings[0].to_dict()


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
