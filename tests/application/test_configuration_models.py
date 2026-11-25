"""Configuration value objects, plugin identity, and module/engine setting models."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import pickle
import subprocess
import sys
from uuid import uuid4

import pytest

from omnibci.application.configuration.errors import PluginProfileValidationError
from omnibci.application.configuration.models import (
    ActivationSet,
    EngineSettings,
    ModuleActivation,
    ModuleActivationRevision,
    PluginProfile,
    ProfileBinding,
    ProfileRevision,
    module_for_slot,
)
from omnibci.application.plugin_management.identity import PluginSelection
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


@pytest.mark.parametrize("module,key,value", [
    ("processing", "ALGORITHM.baseline_duration", -1),
    ("processing", "ALGORITHM.baseline_duration", True),
    ("processing", "ALGORITHM.baseline_duration", "1"),
    ("processing", "ALGORITHM.baseline_duration", float("inf")),
    ("processing", "ALGORITHM.baseline_duration", 10 ** 400),
    ("stimulation", "STIMULATION.renderer", "missing"),
    ("stimulation", "STIMULATION.renderer", []),
    ("stimulation", "STIMULATION.auto_spawn", 1),
    ("actuation", "ACTUATION.auto_spawn", "false"),
    ("actuation", "ACTUATION.server_ip", "  "),
    ("actuation", "ACTUATION.server_ip", 127),
    ("actuation", "ACTUATION.server_port", 0),
    ("actuation", "ACTUATION.server_port", 65536),
    ("actuation", "ACTUATION.server_port", True),
    ("actuation", "ACTUATION.watchdog_timeout_s", 0),
    ("actuation", "ACTUATION.watchdog_timeout_s", float("nan")),
    ("actuation", "ACTUATION.max_command_age_s", -1),
    ("actuation", "ACTUATION.max_command_age_s", False),
    ("acquisition", "ALGORITHM.baseline_duration", 0),
])
def test_invalid_module_setting_is_rejected_before_storage(module, key, value):
    with pytest.raises(PluginProfileValidationError):
        ModuleActivation(module, {key: value}, ())


@pytest.mark.parametrize("module,settings", [
    ("acquisition", {}),
    ("processing", {"ALGORITHM.baseline_duration": 0}),
    ("stimulation", {"STIMULATION.renderer": "pyopengl", "STIMULATION.auto_spawn": False}),
    ("stimulation", {"STIMULATION.renderer": "moderngl"}),
    ("actuation", {"ACTUATION.server_ip": "device.local", "ACTUATION.server_port": 65535,
                   "ACTUATION.watchdog_timeout_s": 0.1, "ACTUATION.max_command_age_s": 0,
                   "ACTUATION.auto_spawn": True}),
])
def test_owned_module_settings_preserve_supported_values(module, settings):
    assert ModuleActivation(module, settings, ()).settings == settings


@pytest.mark.parametrize("module", ["stimulation", "actuation"])
@pytest.mark.parametrize("field,value", [
    ("server_ip", "  "), ("server_ip", 127),
    ("server_port", 0), ("server_port", 65536), ("server_port", True),
    ("server_port", "8765"), ("server_port", 8765.5),
])
def test_output_transport_values_are_validated_in_the_owning_module(module, field, value):
    key = f"{module.upper()}.{field}"
    with pytest.raises(PluginProfileValidationError, match="Invalid module setting"):
        ModuleActivation(module, {key: value}, ())


@pytest.mark.parametrize("key,value", [
    ("NEUROENGINE.queue_size", True), ("NEUROENGINE.queue_size", 0),
    ("NEUROENGINE.queue_size", 1.5), ("NEUROENGINE.queue_size", "100"),
    ("NEUROENGINE.startup_timeout_s", False), ("NEUROENGINE.startup_timeout_s", 0),
    ("NEUROENGINE.startup_timeout_s", -1), ("NEUROENGINE.startup_timeout_s", float("inf")),
    ("NEUROENGINE.startup_timeout_s", 10 ** 400), ("debug", 1), ("debug", "false"),
])
def test_invalid_engine_values_cannot_be_accepted(key, value):
    with pytest.raises(PluginProfileValidationError):
        EngineSettings({key: value})


def test_identity_import_loads_no_other_project_modules():
    result = subprocess.run([sys.executable, "-B", "-c", """
import sys
from omnibci.application.plugin_management.identity import PluginSelection, PLUGIN_KINDS

assert PluginSelection('acme.test', 'main', '1.0.0').version == '1.0.0'
assert 'effector' in PLUGIN_KINDS
assert {name for name in sys.modules if name.startswith('omnibci.')} == {
    'omnibci.application',
    'omnibci.application.plugin_management',
    'omnibci.application.plugin_management.identity',
}
"""], cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_selection_round_trips_and_cannot_be_mutated():
    data = {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0"}
    selection = PluginSelection.from_dict(data)
    assert selection.to_dict() == data
    assert pickle.loads(pickle.dumps(selection)) == selection
    assert hash(selection) == hash(PluginSelection(**data))
    data["version"] = "2.0.0"
    selection.to_dict()["version"] = "3.0.0"
    assert selection.version == "1.0.0"
    with pytest.raises(FrozenInstanceError):
        selection.version = "4.0.0"


@pytest.mark.parametrize("version", ["1", "0.0.0", "1beta", "1.2.3-rc.1+build.4", "1" * 128])
def test_existing_exact_version_spellings_are_preserved(version):
    selection = PluginSelection("acme.test", "main", version)
    assert PluginSelection.from_dict(selection.to_dict()) == selection


@pytest.mark.parametrize("version", [None, "", "*", ">=1", "1.0.*", " 1.0.0", "1.0.0.", "1" * 129, "１.0.0"])
def test_selections_require_exact_versions(version):
    with pytest.raises(ValueError) as raised:
        PluginSelection("acme.test", "main", version)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize(("field", "value"), [
    ("package_id", None), ("package_id", True), ("package_id", ""),
    ("package_id", " acme.test"), ("contribution_id", 1), ("contribution_id", "main "),
])
def test_selection_fields_reject_coercion_or_trimming(field, value):
    data = {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0", field: value}
    with pytest.raises(ValueError) as raised:
        PluginSelection.from_dict(data)
    assert type(raised.value) is ValueError


@pytest.mark.parametrize("data", [
    None, [], {}, {"package_id": "acme.test", "version": "1.0.0"},
    {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0", "kind": "effector"},
    {"package_id": "acme.test", "contribution_id": "main", "version": "1.0.0", 0: "extra"},
])
def test_selection_documents_require_exact_fields(data):
    with pytest.raises(ValueError, match="fields") as raised:
        PluginSelection.from_dict(data)
    assert type(raised.value) is ValueError
