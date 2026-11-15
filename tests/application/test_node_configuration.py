"""A node validates only its pinned capabilities and shared runtime context."""
from dataclasses import replace

import pytest

from omnibci.application.node_configuration import (
    NodeConfiguration, materialize_node_configuration, project_node_configuration,
)
from omnibci.application.plugin_profiles import ProfileBinding, PluginSelection
from omnibci.application.processing_source import processing_input as selected_processing_input
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.domain.channel_mapping import ProcessingInput, ChannelBinding


SLOTS = {
    "acquisition": "acquisition.source", "decoder": "processing.decoder",
    "epoch_detector": "processing.epoch_detector", "paradigm": "stimulation.paradigm",
    "trigger": "stimulation.trigger", "effector": "actuation.effector",
}


def binding(kind, parameters=None):
    return ProfileBinding(
        slot=SLOTS[kind], kind=kind, profile_id=None,
        selection=PluginSelection(f"test.{kind}", kind, "1.2.3"),
        parameters=parameters or {}, processing_input=ProcessingInput() if kind == "decoder" else None,
    )


@pytest.fixture
def runtime():
    bindings = [binding(kind) for kind in SLOTS]
    bindings[0] = binding("acquisition", {
        "sample_rate": 1000, "channel_count": 2, "data_file_path": "private/source.cnt",
    })
    return {
        "CONFIG.module_revisions": {'acquisition': "a" * 64, 'processing': "a" * 64, 'stimulation': "a" * 64, 'actuation': "a" * 64}, "CONFIG.system_revision": "c" * 64, "CONFIG.generation": "engine-generation-1",
        "ACQUISITION.sample_rate": 1000, "ACQUISITION.channel_count": 2,
        "NEUROENGINE.message_server_ip": "192.0.2.1", "NEUROENGINE.message_server_port": 8765,
        "NEUROENGINE.queue_size": 100, "STIMULATION.renderer": "pyopengl",
        "STIMULATION.auto_spawn": False, "ACTUATION.auto_spawn": False,
        "ACTUATION.enabled": True, "ACTUATION.server_ip": "192.0.2.1",
        "ACTUATION.server_port": 8001, "ACTUATION.watchdog_timeout_s": 1.0,
        "ACTUATION.max_command_age_s": 0.25,
        "PLUGINS.bindings": [item.to_dict() for item in bindings],
        "LOGGING.file_path": "private/log", "custom.private_key": "never send",
        "ACQUISITION.signal_source": "incorrect-alias", "STIMULATION.paradigm": "incorrect-alias",
    }


class LocalCatalog:
    def __init__(self, kinds):
        self.calls = []
        self.definitions = {
            kind: PluginConfiguration(
                selection=binding(kind).selection, kind=kind, domain=SLOTS[kind].split(".")[0], plugin_type=f"local_{kind}",
                label=kind, description=None, available=True, configuration_version=1,
                schema={"type": "object", "properties": {}, "additionalProperties": False},
            ) for kind in kinds
        }
        if "acquisition" in self.definitions:
            self.definitions["acquisition"] = replace(self.definitions["acquisition"], schema={
                "type": "object", "additionalProperties": False,
                "properties": {"sample_rate": {"type": "integer", "minimum": 1},
                               "channel_count": {"type": "integer", "minimum": 1},
                               "channel_labels": {"type": "array", "items": {"type": "string"}},
                               "data_file_path": {"type": "string"}},
            })

    def get(self, kind, selection):
        self.calls.append((kind, selection))
        return self.definitions[kind]


@pytest.mark.parametrize(("role", "kinds", "alias"), [
    ("stimulation", {"paradigm", "trigger"}, "STIMULATION.paradigm"),
    ("actuation", {"effector"}, "ACTUATION.effector"),
    ("engine", {"acquisition", "decoder", "epoch_detector"}, "ACQUISITION.signal_source"),
])
def test_node_needs_only_owned_plugins(runtime, role, kinds, alias):
    catalog = LocalCatalog(kinds)
    dto = project_node_configuration(runtime, role)
    result = materialize_node_configuration(NodeConfiguration.from_dict(dto.to_dict()), catalog)

    assert {kind for kind, _ in catalog.calls} == kinds
    assert {item["kind"] for item in result["PLUGINS.bindings"]} == kinds
    assert result[alias].startswith("local_")
    assert result["CONFIG.system_revision"] == runtime["CONFIG.system_revision"]
    assert result["CONFIG.generation"] == runtime["CONFIG.generation"]
    assert result["CONFIG.node_role"] == role
    if role == "engine":
        assert result["ACQUISITION.sample_rate"] == 1000
        assert result["ACQUISITION.channel_count"] == 2
    else:
        assert "ACQUISITION.sample_rate" not in result
        assert "ACQUISITION.channel_count" not in result
    assert "ACQUISITION.channel_names" not in result
    assert "LOGGING.file_path" not in result
    assert "custom.private_key" not in result
    if role != "engine":
        assert "ACQUISITION.data_file_path" not in result
        assert "NEUROENGINE.queue_size" not in result
    if role == "stimulation":
        assert "ACTUATION.server_ip" not in result
    if role == "actuation":
        assert "STIMULATION.renderer" not in result
        assert "NEUROENGINE.message_server_ip" not in result


def test_stimulation_node_preserves_an_explicit_absent_trigger(runtime):
    runtime["PLUGINS.bindings"] = [item for item in runtime["PLUGINS.bindings"] if item["kind"] != "trigger"]
    runtime["ACQUISITION.trigger"] = "stale-hardware-alias"
    catalog = LocalCatalog({"paradigm"})
    dto = project_node_configuration(runtime, "stimulation")
    result = materialize_node_configuration(NodeConfiguration.from_dict(dto.to_dict()), catalog)
    assert {kind for kind, _ in catalog.calls} == {"paradigm"}
    assert {item["slot"] for item in result["PLUGINS.bindings"]} == {"stimulation.paradigm"}
    assert "ACQUISITION.trigger" not in result
    assert result["CONFIG.system_revision"] == runtime["CONFIG.system_revision"]
    assert result["CONFIG.generation"] == runtime["CONFIG.generation"]


def test_stimulation_node_rejects_an_explicit_unavailable_trigger(runtime):
    catalog = LocalCatalog({"paradigm", "trigger"})
    catalog.definitions["trigger"] = replace(catalog.definitions["trigger"], available=False)
    with pytest.raises(PluginSchemaError, match="stimulation.trigger: unavailable"):
        materialize_node_configuration(project_node_configuration(runtime, "stimulation"), catalog)







def test_projection_requires_no_installed_catalog_and_preserves_parent_identity(runtime):
    dto = project_node_configuration(runtime, "stimulation")
    assert dto.role == "stimulation"
    assert dto.module_revisions == {"stimulation": runtime["CONFIG.module_revisions"]["stimulation"]}
    assert dto.system_revision == runtime["CONFIG.system_revision"]
    assert dto.generation == runtime["CONFIG.generation"]
    assert dto.to_dict()["format_version"] == 1
    assert dto.settings["STIMULATION.auto_spawn"] is False
    runtime.pop("CONFIG.generation")
    assert project_node_configuration(runtime, "stimulation").generation is None






@pytest.mark.parametrize("processing_input", [
    ProcessingInput(),
    ProcessingInput(outputs=(ChannelBinding("O2", "channel-1"), ChannelBinding("O1", "channel-0"))),
])
def test_decoder_input_overrides_legacy_setup_using_pinned_source(runtime, processing_input):
    runtime["SIGNAL.setup"] = {"source_id": "old-device", "outputs": ["obsolete"]}
    decoder = ProfileBinding.from_dict(runtime["PLUGINS.bindings"][1])
    runtime["PLUGINS.bindings"][1] = replace(decoder, processing_input=processing_input).to_dict()
    dto = NodeConfiguration.from_dict(project_node_configuration(runtime, "engine").to_dict())
    result = materialize_node_configuration(dto, LocalCatalog({"acquisition", "decoder", "epoch_detector"}))
    assert selected_processing_input(result) == processing_input
    assert "SIGNAL.setup" not in result
    assert result["ACQUISITION.signal_source"] == "local_acquisition"
    assert dto.bindings[1].processing_input == processing_input
    assert "SIGNAL.setup" not in project_node_configuration(runtime, "stimulation").settings


def test_projected_dto_is_independent_of_input_and_export_mutation(runtime):
    runtime["PLUGINS.bindings"][0]["parameters"]["channel_labels"] = ["C3", "Trigger"]
    runtime["ACQUISITION.channel_labels"] = ["C3", "Trigger"]
    dto = project_node_configuration(runtime, "engine")
    expected = dto.to_dict()
    runtime["ACQUISITION.channel_labels"].clear()
    exported = dto.to_dict()
    exported["settings"]["ACQUISITION.channel_labels"].clear()
    exported["bindings"][0]["parameters"]["injected"] = True
    dto.settings.clear()
    assert dto.to_dict() == expected


@pytest.mark.parametrize("change", ["schema_version", "unresolved_default", "unknown_parameter", "unavailable", "selection"])
def test_local_validation_rejects_unusable_owned_configuration(runtime, change):
    catalog = LocalCatalog({"paradigm", "trigger"})
    definition = catalog.definitions["paradigm"]
    if change == "schema_version":
        definition = replace(definition, configuration_version=2)
    elif change == "unresolved_default":
        definition = replace(definition, schema={
            "type": "object", "additionalProperties": False,
            "properties": {"duration": {"type": "number", "default": 1.0}},
        })
    elif change == "unknown_parameter":
        next(item for item in runtime["PLUGINS.bindings"] if item["kind"] == "paradigm")["parameters"]["unknown"] = 1
    elif change == "unavailable":
        definition = replace(definition, available=False)
    elif change == "selection":
        definition = replace(definition, selection=replace(definition.selection, version="9.9.9"))
    catalog.definitions["paradigm"] = definition
    with pytest.raises(PluginSchemaError):
        materialize_node_configuration(project_node_configuration(runtime, "stimulation"), catalog)


@pytest.mark.parametrize("value", [0, -1, True, 1000.0, "1000"])
def test_engine_rejects_invalid_sampling_context(runtime, value):
    runtime["ACQUISITION.sample_rate"] = value
    runtime["PLUGINS.bindings"][0]["parameters"]["sample_rate"] = value
    with pytest.raises(PluginSchemaError, match="sample_rate"):
        project_node_configuration(runtime, "engine")


def test_flat_context_cannot_disagree_with_pinned_source(runtime):
    runtime["ACQUISITION.sample_rate"] = 250
    with pytest.raises(PluginSchemaError, match="sample_rate"):
        project_node_configuration(runtime, "engine")


@pytest.mark.parametrize("role", ["engine", "stimulation", "actuation"])
@pytest.mark.parametrize("names", [["obsolete", "names"], None])
def test_node_projection_ignores_retired_flat_channel_names(runtime, role, names):
    expected = project_node_configuration(runtime, role).to_dict()
    runtime["ACQUISITION.channel_names"] = names

    assert project_node_configuration(runtime, role).to_dict() == expected


@pytest.mark.parametrize("role", ["engine", "stimulation", "actuation"])
def test_received_node_settings_reject_retired_flat_channel_names(runtime, role):
    data = project_node_configuration(runtime, role).to_dict()
    data["settings"]["ACQUISITION.channel_names"] = ["obsolete", "names"]

    with pytest.raises(PluginSchemaError, match="unsupported fields"):
        NodeConfiguration.from_dict(data)


@pytest.mark.parametrize(("role", "kinds"), [
    ("engine", {"acquisition", "decoder", "epoch_detector"}),
    ("stimulation", {"paradigm", "trigger"}),
    ("actuation", {"effector"}),
])
def test_only_engine_receives_native_channel_labels(runtime, role, kinds):
    labels = ["C3", "Trigger"]
    runtime["PLUGINS.bindings"][0]["parameters"]["channel_labels"] = labels
    dto = NodeConfiguration.from_dict(project_node_configuration(runtime, role).to_dict())
    result = materialize_node_configuration(dto, LocalCatalog(kinds))
    if role == "engine":
        assert dto.settings["ACQUISITION.channel_labels"] == labels
        assert result["ACQUISITION.channel_labels"] == labels
    else:
        assert "ACQUISITION.channel_labels" not in dto.settings
        assert "ACQUISITION.channel_labels" not in result


@pytest.mark.parametrize("stage", ["projection", "materialization"])
def test_channel_labels_cannot_disagree_with_pinned_source(runtime, stage):
    runtime["PLUGINS.bindings"][0]["parameters"]["channel_labels"] = ["C3", "Trigger"]
    if stage == "projection":
        runtime["ACQUISITION.channel_labels"] = ["C4", "Trigger"]
        with pytest.raises(PluginSchemaError, match="channel_labels.*disagrees"):
            project_node_configuration(runtime, "engine")
    else:
        data = project_node_configuration(runtime, "engine").to_dict()
        data["settings"]["ACQUISITION.channel_labels"] = ["C4", "Trigger"]
        dto = NodeConfiguration.from_dict(data)
        with pytest.raises(PluginSchemaError, match="channel_labels.*disagrees"):
            materialize_node_configuration(dto, LocalCatalog({"acquisition", "decoder", "epoch_detector"}))


@pytest.mark.parametrize("labels", ["C3,Trigger", None, ["C3", 1]])
def test_node_channel_labels_must_be_a_list_of_strings(runtime, labels):
    data = project_node_configuration(runtime, "engine").to_dict()
    data["settings"]["ACQUISITION.channel_labels"] = labels
    with pytest.raises(PluginSchemaError, match="channel_labels.*list of strings"):
        NodeConfiguration.from_dict(data)


@pytest.mark.parametrize("change", ["foreign_binding", "foreign_setting", "unknown_field", "version", "revision", "generation", "role", "context", "missing_binding", "duplicate_binding"])
def test_remote_envelope_rejects_invalid_or_unowned_content(runtime, change):
    data = project_node_configuration(runtime, "stimulation").to_dict()
    if change == "foreign_binding":
        data["bindings"].append(binding("decoder").to_dict())
    elif change == "foreign_setting":
        data["settings"]["ACTUATION.server_ip"] = "192.0.2.2"
    elif change == "unknown_field":
        data["extra"] = True
    elif change == "version":
        data["format_version"] = True
    elif change == "revision":
        data["module_revisions"][next(iter(data["module_revisions"]))] = "not-a-revision"
    elif change == "generation":
        data["generation"] = ""
    elif change == "role":
        data["role"] = "arbitrary"
    elif change == "context":
        data["settings"]["ACQUISITION.channel_count"] = True
    elif change == "missing_binding":
        data["bindings"].clear()
    elif change == "duplicate_binding":
        data["bindings"].append(data["bindings"][0])
    with pytest.raises((PluginSchemaError, ValueError)):
        NodeConfiguration.from_dict(data)


def test_actuation_configuration_does_not_require_a_separate_enable_flag(runtime):
    runtime.pop("ACTUATION.enabled")
    dto = project_node_configuration(runtime, "actuation")
    assert "ACTUATION.enabled" not in dto.settings
    assert dto.bindings[0].slot == "actuation.effector"


def test_source_metadata_can_be_derived_without_flat_aliases(runtime):
    runtime.pop("ACQUISITION.sample_rate")
    runtime.pop("ACQUISITION.channel_count")
    dto = project_node_configuration(runtime, "engine")
    assert dto.settings["ACQUISITION.sample_rate"] == 1000
    assert dto.settings["ACQUISITION.channel_count"] == 2
    assert "ACQUISITION.channel_names" not in dto.settings


@pytest.mark.parametrize(("role", "kinds"), [
    ("engine", {"acquisition", "decoder", "epoch_detector"}),
    ("stimulation", {"paradigm", "trigger"}),
    ("actuation", {"effector"}),
])
def test_nodes_can_start_before_source_expectations_are_confirmed(runtime, role, kinds):
    for name in ("sample_rate", "channel_count"):
        runtime.pop(f"ACQUISITION.{name}")
        runtime["PLUGINS.bindings"][0]["parameters"].pop(name)
    dto = NodeConfiguration.from_dict(project_node_configuration(runtime, role).to_dict())
    result = materialize_node_configuration(dto, LocalCatalog(kinds))
    assert "ACQUISITION.sample_rate" not in dto.settings
    assert "ACQUISITION.channel_count" not in dto.settings
    assert "ACQUISITION.sample_rate" not in result
    assert "ACQUISITION.channel_count" not in result
    assert result["CONFIG.system_revision"] == runtime["CONFIG.system_revision"]


def test_nonowned_plugin_parameters_never_receive_local_schema_validation(runtime):
    runtime["PLUGINS.bindings"][1]["parameters"] = {"foreign_only_parameter": [1, 2, 3]}
    result = materialize_node_configuration(
        project_node_configuration(runtime, "actuation"), LocalCatalog({"effector"}),
    )
    assert [item["kind"] for item in result["PLUGINS.bindings"]] == ["effector"]


@pytest.mark.parametrize(("role", "kinds"), [
    ("stimulation", {"paradigm", "trigger"}), ("actuation", {"effector"}),
])
def test_output_nodes_run_without_acquisition_or_processing_configuration(runtime, role, kinds):
    runtime["PLUGINS.bindings"] = [item for item in runtime["PLUGINS.bindings"] if item["kind"] in kinds]
    for key in tuple(runtime):
        if key.startswith("ACQUISITION."):
            runtime.pop(key)
    runtime["CONFIG.module_revisions"] = {role: runtime["CONFIG.module_revisions"][role]}
    dto = project_node_configuration(runtime, role)
    result = materialize_node_configuration(NodeConfiguration.from_dict(dto.to_dict()), LocalCatalog(kinds))
    assert {item["kind"] for item in result["PLUGINS.bindings"]} == kinds
    assert not any(key.startswith("ACQUISITION.") for key in dto.settings)


@pytest.mark.parametrize("role", ["stimulation", "actuation"])
def test_output_projection_ignores_unrelated_source_context(runtime, role):
    expected = project_node_configuration(runtime, role).to_dict()
    runtime["ACQUISITION.sample_rate"] = "invalid foreign context"
    runtime["PLUGINS.bindings"][0]["parameters"] = None
    assert project_node_configuration(runtime, role).to_dict() == expected


@pytest.mark.parametrize("change", ["missing", "no_schema"])
def test_owned_plugin_must_be_installed_with_a_schema(runtime, change):
    catalog = LocalCatalog({"effector"})
    if change == "missing":
        catalog.definitions.clear()
    else:
        catalog.definitions["effector"] = replace(catalog.definitions["effector"], schema=None)
    with pytest.raises((KeyError, PluginSchemaError)):
        materialize_node_configuration(project_node_configuration(runtime, "actuation"), catalog)


def test_received_engine_source_must_match_declared_context(runtime):
    data = project_node_configuration(runtime, "engine").to_dict()
    data["settings"]["ACQUISITION.sample_rate"] = 250
    dto = NodeConfiguration.from_dict(data)
    with pytest.raises(PluginSchemaError, match="sample_rate"):
        materialize_node_configuration(dto, LocalCatalog({"acquisition", "decoder", "epoch_detector"}))


@pytest.mark.parametrize(("key", "value"), [
    ("ACTUATION.server_port", True), ("ACTUATION.server_port", 0),
    ("ACTUATION.server_ip", ""), ("ACTUATION.watchdog_timeout_s", float("inf")),
    ("ACTUATION.max_command_age_s", -1), ("ACTUATION.auto_spawn", "false"),
    ("ACQUISITION.channel_labels", [123]),
])
def test_node_transport_and_timing_settings_are_validated(runtime, key, value):
    data = project_node_configuration(runtime, "actuation").to_dict()
    data["settings"][key] = value
    with pytest.raises(PluginSchemaError):
        NodeConfiguration.from_dict(data)


def test_legacy_global_setup_is_not_projected_to_nodes(runtime):
    runtime["SIGNAL.setup"] = {"source_id": "test-source", "outputs": ["O1"]}
    assert "SIGNAL.setup" not in project_node_configuration(runtime, "engine").settings
    assert "SIGNAL.setup" not in project_node_configuration(runtime, "stimulation").settings
