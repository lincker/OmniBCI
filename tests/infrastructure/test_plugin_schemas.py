"""Configuration schemas are static package data and are checked before execution."""
import json

import pytest
from tests.plugin_configuration_helpers import output_payload_files
import tomlkit

from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.schema import PluginSchemaError, validate_parameters
from omnibci.infrastructure.plugin_management.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugin_management.catalog import (
    PluginCatalog, PluginRoot, vendor_plugins_dir,
)
from omnibci.infrastructure.plugin_management.manifest import PluginManifestError
from omnibci.sdk import Effector


SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"gain": {"type": "number", "minimum": 0, "default": 1}},
    "required": ["gain"],
}


def package(tmp_path, **configuration):
    manifest = {"manifest_version": 1, "package": {
        "id": "example", "publisher": "test", "version": "1", "api_version": "1", "domain": "actuation",
    }, "contributions": [{
        "id": "output", "kind": "effector", "entrypoint": "output:Output",
        "configuration": configuration or {"schema": "default.schema.json", "schema_version": 1},
    }]}
    for name, contents in output_payload_files(manifest).items():
        (tmp_path / name).write_text(contents)
    (tmp_path / "manifest.toml").write_text(tomlkit.dumps(manifest), encoding="utf-8")
    (tmp_path / "default.schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
    return PluginCatalog({"effector": lambda: [PluginRoot(tmp_path, "user", 0)]})


def test_all_builtins_own_valid_defaults_for_their_required_parameters():
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)]})
    count = 0
    for kind in ("acquisition", "decoder", "filter", "epoch_detector", "effector", "paradigm", "trigger"):
        for plugin in catalog.list_contributions(kind):
            assert plugin.configuration_version == 1
            values = validate_parameters(plugin.configuration_schema, {})
            assert set(plugin.configuration_schema.get("required", [])) <= set(values)
            assert set(values) <= set(plugin.configuration_schema["properties"])
            count += 1
    assert count == 13


@pytest.mark.parametrize("alias", ["file_reader", "neuroscan_curry8_reader"])
def test_observed_sources_preserve_optional_expectations_without_an_invented_baseline(alias):
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)]})
    schema = catalog.resolve_selection("acquisition", PluginSelection(f"OmniBCI.{alias}", "default", "0.1.0")).configuration_schema
    defaults = validate_parameters(schema, {})
    assert not {"sample_rate", "channel_count"} & defaults.keys()
    saved = validate_parameters(schema, {"sample_rate": 500, "channel_count": 33})
    assert saved["sample_rate"] == 500
    assert saved["channel_count"] == 33


def test_scan45_retains_required_stream_dimensions_and_defaults():
    catalog = PluginCatalog({"effector": lambda: [PluginRoot(vendor_plugins_dir(), "builtin", 100)]})
    schema = catalog.resolve_selection("acquisition", PluginSelection("OmniBCI.neuroscan_scan45_reader", "default", "0.1.0")).configuration_schema
    assert {"sample_rate", "channel_count"} <= set(schema["required"])
    defaults = validate_parameters(schema, {})
    assert defaults["sample_rate"] == 1000
    assert defaults["channel_count"] == 10


def test_curry8_labels_are_optional_and_preserve_all_native_rows():
    schema = PluginCatalog().resolve_selection("acquisition", PluginSelection("OmniBCI.neuroscan_curry8_reader", "default", "0.1.0")).configuration_schema
    assert "channel_labels" not in validate_parameters(schema, {})
    labels = ["PZ", "PO5", "PO3", "POZ", "PO4", "PO6", "O1", "OZ", "O2", "Trigger"]
    assert validate_parameters(schema, {"channel_count": 10, "channel_labels": labels})["channel_labels"] == labels
    assert validate_parameters(schema, {"channel_labels": ["", "OZ", "OZ", "Trigger"]})["channel_labels"] == [
        "", "OZ", "OZ", "Trigger",
    ]


@pytest.mark.parametrize("labels", ["OZ,Trigger", None, ["OZ", 1], [True, "Trigger"]])
def test_curry8_labels_require_an_array_of_strings(labels):
    schema = PluginCatalog().resolve_selection("acquisition", PluginSelection("OmniBCI.neuroscan_curry8_reader", "default", "0.1.0")).configuration_schema
    with pytest.raises(PluginSchemaError, match="channel_labels.*must be"):
        validate_parameters(schema, {"channel_labels": labels})


@pytest.mark.parametrize("alias", ["file_reader", "neuroscan_curry8_reader", "neuroscan_scan45_reader"])
def test_acquisition_schemas_use_channel_count_and_reject_the_old_parameter(alias):
    schema = PluginCatalog().resolve_selection("acquisition", PluginSelection(f"OmniBCI.{alias}", "default", "0.1.0")).configuration_schema
    assert "channel_count" in schema["properties"]
    assert "channel_number" not in schema["properties"]
    assert validate_parameters(schema, {"channel_count": 33})["channel_count"] == 33
    with pytest.raises(PluginSchemaError, match="channel_number"):
        validate_parameters(schema, {"channel_number": 33})


@pytest.mark.parametrize("configuration", [
    {"schema": "../outside.json", "schema_version": 1},
    {"schema": "/outside.json", "schema_version": 1},
    {"schema": "C:\\outside.json", "schema_version": 1},
    {"schema": "missing.json", "schema_version": 1},
    {"schema": "default.schema.json", "schema_version": True},
    {"schema": "default.schema.json"},
])
def test_invalid_schema_declaration_blocks_package(tmp_path, configuration):
    catalog = package(tmp_path, **configuration)
    with pytest.raises(PluginManifestError, match="configuration"):
        catalog.resolve_selection("effector", PluginSelection("test.example", "output", "1"))


@pytest.mark.parametrize("content", [
    '{"type":"object","type":"object"}',
    '{"type":"object","additionalProperties":false,"$ref":"https://example.com"}',
    '{"type":"number"}', '{"type":',
])
def test_malformed_schema_blocks_package(tmp_path, content):
    catalog = package(tmp_path)
    (tmp_path / "default.schema.json").write_text(content, encoding="utf-8")
    with pytest.raises(PluginManifestError, match="configuration"):
        catalog.resolve_selection("effector", PluginSelection("test.example", "output", "1"))


def test_schema_symlink_is_rejected(tmp_path):
    catalog = package(tmp_path)
    schema = tmp_path / "default.schema.json"
    schema.rename(tmp_path / "real.json")
    schema.symlink_to(tmp_path / "real.json")
    with pytest.raises(PluginManifestError, match="symlinks"):
        catalog.resolve_selection("effector", PluginSelection("test.example", "output", "1"))


def test_invalid_parameters_fail_before_plugin_code_runs(tmp_path, monkeypatch):
    selected = package(tmp_path).resolve_selection("effector", PluginSelection("test.example", "output", "1"))
    monkeypatch.setattr(PluginLoader, "load_entrypoint", lambda *a: pytest.fail("imported"))
    with pytest.raises(PluginLoadError, match="gain"):
        PluginLoader.instantiate(selected, Effector, parameters={"gain": True})


TRIAL_SCHEMA = {
    "type": "object",
    "properties": {"event_id": {"type": "integer", "minimum": 0, "maximum": 40}},
    "required": ["event_id"],
    "additionalProperties": False,
}


def _payload_binding(slot, kind, alias):
    from omnibci.application.configuration.models import ProfileBinding
    from tests.plugin_configuration_helpers import builtin_selection

    descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
    return ProfileBinding(slot, kind, None, descriptor.selection, {}).to_dict()


def _parse_payload_package(tmp_path, kind, declaration):
    from omnibci.infrastructure.plugin_management.manifest import parse_manifest
    from tests.infrastructure.test_package_installer import _manifest

    document = _manifest()
    contribution = document["contributions"][0]
    contribution["kind"] = kind
    if declaration is not None:
        contribution["payload_schemas"] = declaration
    if kind == "decoder":
        contribution["channel_requirements"] = {}
    return parse_manifest(document, tmp_path, source="user", precedence=0, host_version="1.0.0")


@pytest.mark.parametrize("kind,fields", [("paradigm", ("trial", "result")), ("effector", ("command",))])
def test_payload_schema_paths_are_static_local_role_declarations(tmp_path, kind, fields):
    (tmp_path / "payload.json").write_text(json.dumps(TRIAL_SCHEMA))
    (tmp_path / "driver.py").write_text("raise AssertionError('Static validation imported code')\n")
    descriptor, = _parse_payload_package(tmp_path, kind, dict.fromkeys(fields, "payload.json"))
    assert set(descriptor.payload_schemas) == set(fields)
    assert all(schema == TRIAL_SCHEMA for schema in descriptor.payload_schemas.values())


@pytest.mark.parametrize("kind,declaration", [
    ("paradigm", None), ("paradigm", {}), ("paradigm", {"trial": "payload.json"}),
    ("paradigm", {"trial": "payload.json", "result": "payload.json", "command": "payload.json"}),
    ("effector", None), ("effector", {}), ("effector", {"result": "payload.json"}),
])
def test_output_roles_require_exact_payload_schema_declarations(tmp_path, kind, declaration):
    (tmp_path / "payload.json").write_text(json.dumps(TRIAL_SCHEMA))
    with pytest.raises(PluginManifestError, match="payload_schemas"):
        _parse_payload_package(tmp_path, kind, declaration)


@pytest.mark.parametrize("kind", ["acquisition", "trigger", "decoder", "filter", "epoch_detector"])
def test_other_roles_cannot_declare_runtime_payload_schemas(tmp_path, kind):
    with pytest.raises(PluginManifestError, match="payload_schemas"):
        _parse_payload_package(tmp_path, kind, {})


@pytest.mark.parametrize("path,contents", [
    ("../outside.json", "{}"), ("/outside.json", "{}"), ("bad\\path.json", "{}"),
    ("missing.json", None), ("payload.json", '{"type":"object","type":"object"}'),
    ("payload.json", '{"$ref":"https://example.test/schema"}'),
    ("payload.json", " " * (1024 * 1024 + 1)),
], ids=["parent", "absolute", "backslash", "missing", "duplicate-key", "external-reference", "oversize"])
def test_payload_schemas_reuse_bounded_local_schema_validation(tmp_path, path, contents):
    if contents is not None:
        (tmp_path / "payload.json").write_text(contents)
    with pytest.raises(PluginManifestError, match="payload_schemas"):
        _parse_payload_package(tmp_path, "effector", {"command": path})


def _stimulator():
    from unittest.mock import Mock
    from omnibci.infrastructure.stimulation.stimulator import Stimulator
    from tests.plugin_configuration_helpers import prepared_snapshot

    owner = Stimulator(settings_snapshot=prepared_snapshot({"PLUGINS.bindings": [
        _payload_binding("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ]}))
    owner._load_paradigm()
    owner.paradigm = Mock()
    owner.paradigm.present_trial.return_value = owner.paradigm.present_result.return_value = None
    return owner


@pytest.mark.parametrize("method,valid", [("present_trial", {"event_id": 0}), ("present_result", {"target_id": 3})])
def test_stimulation_dispatches_copied_objects_to_only_the_v1_method(method, valid):
    owner = _stimulator()
    getattr(owner, method)(valid)
    callback = getattr(owner.paradigm, method)
    callback.assert_called_once_with(valid)
    assert callback.call_args.args[0] is not valid


@pytest.mark.parametrize("method,value", [
    ("present_trial", None), ("present_trial", 1), ("present_trial", {}),
    ("present_trial", {"event_id": True}), ("present_trial", {"event_id": -1}),
    ("present_trial", {"event_id": 41}), ("present_trial", {"event_id": 1, "revision": "private"}),
    ("present_result", 3), ("present_result", {"target_id": 0}),
    ("present_result", {"target_id": 1.5}), ("present_result", {"target_id": True}),
])
def test_stimulation_rejects_bad_payloads_before_the_plugin_callback(method, value):
    owner = _stimulator()
    with pytest.raises(ValueError):
        getattr(owner, method)(value)
    assert not owner.paradigm.mock_calls


@pytest.mark.parametrize("method,value", [("present_trial", {"event_id": 1}), ("present_result", {"target_id": 2})])
def test_stimulation_callbacks_must_return_none(method, value):
    owner = _stimulator()
    getattr(owner.paradigm, method).return_value = False
    with pytest.raises(TypeError, match="None"):
        getattr(owner, method)(value)


@pytest.mark.parametrize("command", [None, {"target_id": True}, {"target_id": 1.5}, {"target_id": 2, "seq": 9},
                                     {"target_id": 2, "unknown": 1}])
def test_effector_schema_rejects_bad_payloads_before_execute(command):
    from unittest.mock import Mock
    from omnibci.infrastructure.actuation.actuator import Actuator
    from tests.plugin_configuration_helpers import prepared_snapshot

    owner = Actuator(settings_snapshot=prepared_snapshot({"PLUGINS.bindings": [_payload_binding("actuation.effector", "effector", "logger")]}))
    owner.setup()
    callback = owner.effector.execute = Mock(return_value=None)
    try:
        with pytest.raises(ValueError):
            owner.execute(command)
        callback.assert_not_called()
    finally:
        owner.teardown()


def test_optional_decoding_values_are_omitted_from_device_payload_when_absent():
    from omnibci.application.commands.actuation_commands import validate_actuation_command
    from tests.actuation_helpers import actuation_payload

    command = validate_actuation_command(actuation_payload(
        {"target_id": 1, "event_id": None, "confidence": None}, command_id="command-7", issued_at=3.0))
    assert command.parameters == {"target_id": 1}
    assert command.command_id == "command-7" and command.issued_at == 3.0


@pytest.mark.parametrize("factory_name", ["StartStimTrialCommand", "StartTrialRequest"])
@pytest.mark.parametrize("values", [
    {}, {"trial_params": [1]}, {"trial_params": [None]}, {"trial_params": []},
    {"n_trials": True, "trial_params": [{}]}, {"n_trials": "1", "trial_params": [{}]},
    {"n_trials": 1.5, "trial_params": [{}]}, {"n_trials": 0, "trial_params": []},
    {"n_trials": 2, "trial_params": [{}]}, {"trial_params": [{"x": float("nan")}]},
    {"trial_params": [{"x": "x" * (1024 * 1024)}]},
])
def test_trial_batch_requires_bounded_explicit_json_objects(factory_name, values):
    from omnibci.application.commands.stimulation_commands import StartStimTrialCommand
    from omnibci.presentation.schemas.stimulation import StartTrialRequest

    factory = StartStimTrialCommand if factory_name == "StartStimTrialCommand" else StartTrialRequest
    with pytest.raises((TypeError, ValueError)):
        factory(**values)


@pytest.mark.parametrize("factory_name", ["StartStimTrialCommand", "StartTrialRequest"])
def test_trial_batch_preserves_arbitrary_plugin_objects_without_borrowing_input(factory_name):
    from omnibci.application.commands.stimulation_commands import StartStimTrialCommand
    from omnibci.presentation.schemas.stimulation import StartTrialRequest

    factory = StartStimTrialCommand if factory_name == "StartStimTrialCommand" else StartTrialRequest
    parameters = [{"flashes": [1, 3]}, {"flashes": [2, 4]}]
    command = factory(n_trials=2, trial_params=parameters)
    parameters[0]["flashes"].append(9)
    assert command.trial_params == [{"flashes": [1, 3]}, {"flashes": [2, 4]}]
