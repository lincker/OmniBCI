"""Saved profiles and accepted runtime content remain independent."""
import pickle
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import (
    ActivationSet, ModuleActivation, ModuleActivationRevision, PluginProfile,
    PluginProfileValidationError, PluginSelection, ProfileBinding, ProfileRevision,
    EngineSettings, module_for_slot,
)
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput


def _binding(slot="processing.decoder", kind="decoder", **fields):
    if kind == "decoder":
        fields.setdefault("processing_input", ProcessingInput())
    return ProfileBinding(slot, kind, None, PluginSelection("acme.test", "main", "1.0.0"),
                          {"bands": [8, 12]}, **fields)


def test_profile_and_binding_copy_parameters_and_round_trip():
    parameters = {"bands": [8, 12]}
    profile = PluginProfile(str(uuid4()), " Example ", "decoder",
                            PluginSelection("acme.test", "main", "1.0.0"), parameters,
                            processing_input=ProcessingInput())
    binding = _binding()
    parameters["bands"].append(16)
    profile.to_dict()["parameters"]["bands"].append(24)
    assert profile.name == "Example"
    assert profile.parameters == {"bands": [8, 12]}
    assert PluginProfile.from_dict(profile.to_dict()) == profile
    assert ProfileBinding.from_dict(binding.to_dict()) == binding
    assert "profile_id" not in binding.to_dict()


def test_snapshot_cannot_be_changed_by_mutating_inputs_or_getter_values():
    settings = {"ALGORITHM.baseline_duration": 1.0}
    binding = _binding()
    snapshot = ModuleActivation("processing", settings, [binding])
    revision = snapshot.revision
    settings["ALGORITHM.baseline_duration"] = 5.0
    binding.parameters["bands"].append(16)
    snapshot.settings["ALGORITHM.baseline_duration"] = 6.0
    snapshot.bindings[0].parameters["bands"].append(24)
    snapshot.to_dict()["bindings"][0]["parameters"]["bands"].append(32)
    assert snapshot.settings == {"ALGORITHM.baseline_duration": 1.0}
    assert snapshot.bindings[0].parameters == {"bands": [8, 12]}
    assert snapshot.revision == revision
    assert ModuleActivation.from_dict(snapshot.to_dict()) == snapshot
    assert pickle.loads(pickle.dumps(snapshot)) == snapshot


def test_semantic_revision_ignores_profile_provenance_and_parameter_key_order():
    binding = replace(_binding(), parameters={"a": 1, "b": 2})
    original = ModuleActivation("processing", {}, (binding,))
    equivalent = ModuleActivation("processing", {}, (
        replace(binding, parameters={"b": 2, "a": 1}, profile_id=str(uuid4()), profile_revision=8),
    ))
    assert original.revision == equivalent.revision
    assert original.revision != ModuleActivation("processing", {}, (
        replace(binding, parameters={"a": 1, "b": 3}),)).revision
    assert ModuleActivation("acquisition", {}, ()).revision != ModuleActivation("processing", {}, ()).revision


def test_filter_instance_order_is_preserved_and_affects_revision():
    first = _binding(f"processing.filter.{uuid4()}", "filter")
    second = _binding(f"processing.filter.{uuid4()}", "filter")
    original = ModuleActivation("processing", {}, (first, second))
    assert original.bindings == (first, second)
    assert original.revision != ModuleActivation("processing", {}, (second, first)).revision
    with pytest.raises(PluginProfileValidationError, match="Duplicate"):
        ModuleActivation("processing", {}, (first, first))


def test_processing_input_is_decoder_only_and_frozen_in_activation():
    channel_input = ProcessingInput(outputs=(ChannelBinding("O2", "channel-2", "eeg"),))
    binding = replace(_binding(), processing_input=channel_input)
    selected = ModuleActivation("processing", {}, (binding,))
    selected.to_dict()["bindings"][0]["processing_input"]["outputs"].clear()
    assert selected.bindings[0].processing_input == channel_input
    assert ModuleActivation.from_dict(selected.to_dict()) == selected
    with pytest.raises(PluginProfileValidationError, match="decoder"):
        replace(_binding("acquisition.source", "acquisition"), processing_input=channel_input)


@pytest.mark.parametrize(("slot", "kind"), [
    ("processing.decoder", "filter"), ("processing.filter.invalid", "filter"),
    ("acquisition.unknown", "acquisition"), ("../slot", "decoder"),
])
def test_slot_identity_and_kind_are_checked(slot, kind):
    with pytest.raises(PluginProfileValidationError):
        _binding(slot, kind)


def test_module_and_setting_ownership_are_checked():
    assert module_for_slot(f"processing.filter.{uuid4()}") == "processing"
    for module, settings, bindings in (
        ("unknown", {}, ()), ("acquisition", {}, (_binding(),)),
        ("acquisition", {"NEUROENGINE.queue_size": 20}, ()),
        ("processing", {"NEUROENGINE.queue_size": 20}, ()),
        ("actuation", {"ACTUATION.enabled": True}, ()),
    ):
        with pytest.raises(PluginProfileValidationError):
            ModuleActivation(module, settings, bindings)
    with pytest.raises(PluginProfileValidationError):
        EngineSettings({"ACTUATION.auto_spawn": True})


@pytest.mark.parametrize("version", [None, "", "*", ">=1", "1.0.*", " 1.0.0", "1.0.0."])
def test_selections_require_exact_versions(version):
    with pytest.raises(PluginProfileValidationError):
        PluginSelection("acme.test", "main", version)


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


def test_stored_revisions_and_removed_fields_are_verified():
    document = ModuleActivation("processing", {}, (_binding(),)).to_dict()
    for changed in ({**document, "revision": "0" * 64}, {**document, "format_version": 1},
                    {**document, "unexpected": True}):
        with pytest.raises(PluginProfileValidationError):
            ModuleActivation.from_dict(changed)
    with pytest.raises(PluginProfileValidationError, match="revision"):
        EngineSettings.from_dict({"settings": {"debug": True}, "revision": "0" * 64})
    profile = PluginProfile(str(uuid4()), "Source", "acquisition", _binding().selection, {})
    with pytest.raises(PluginProfileValidationError):
        PluginProfile.from_dict({**profile.to_dict(), "manual_montage": {}})
    with pytest.raises(PluginProfileValidationError):
        ProfileBinding.from_dict({**_binding().to_dict(), "processing_input": {"schema_version": 1, "outputs": []}})


def test_activation_set_keeps_absence_explicit_and_copies_mapping():
    acquisition = ModuleActivation("acquisition", {}, ())
    modules = {"acquisition": acquisition}
    selected = ActivationSet(modules, EngineSettings({"NEUROENGINE.queue_size": 20, "debug": False}))
    modules.clear()
    assert selected.modules["acquisition"] == acquisition
    assert selected.modules["processing"] is None
    assert selected.revisions()["processing"] is None
    assert selected.revisions()["engine"] == selected.engine.revision
    assert selected.settings() == {"NEUROENGINE.queue_size": 20, "debug": False}
    with pytest.raises(TypeError):
        selected.modules["processing"] = None


def test_history_entries_preserve_values_and_validate_identity():
    profile = PluginProfile(str(uuid4()), "Source", "acquisition", _binding().selection, {"values": [1]}, revision=1)
    timestamp = "2026-11-15T00:00:00+00:00"
    history = ProfileRevision(profile.profile_id, 1, timestamp, profile)
    profile.parameters["values"].append(2)
    history.profile.parameters["values"].append(3)
    assert history.profile.parameters == {"values": [1]}
    assert ProfileRevision.from_dict(history.to_dict()) == history
    activation = ModuleActivation("processing", {}, (_binding(),))
    entry = ModuleActivationRevision("processing", str(uuid4()), timestamp, activation)
    assert ModuleActivationRevision.from_dict(entry.to_dict()) == entry
    with pytest.raises(PluginProfileValidationError):
        replace(entry, module="acquisition")
    with pytest.raises(PluginProfileValidationError):
        replace(entry, saved_at="2026-11-15")
