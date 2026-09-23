"""Saved profiles and accepted runtime content remain independent."""
import pickle
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginProfileValidationError,
    PluginSelection, ProfileBinding,
)


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
