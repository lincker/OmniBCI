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
        parameters=parameters or {},
    )


@pytest.fixture
def runtime():
    bindings = [binding(kind) for kind in SLOTS]
    bindings[0] = binding("acquisition", {
        "sample_rate": 1000, "channel_number": 2, "data_file_path": "private/source.cnt",
    })
    return {
        "CONFIG.revision": "a" * 64, "CONFIG.generation": "engine-generation-1",
        "ACQUISITION.sample_rate": 1000, "ACQUISITION.channel_number": 2,
        "ACQUISITION.channel_names": ["C3", "C4"],
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
                selection=binding(kind).selection, kind=kind, plugin_type=f"local_{kind}",
                label=kind, description=None, available=True, configuration_version=1,
                schema={"type": "object", "properties": {}, "additionalProperties": False},
            ) for kind in kinds
        }
        if "acquisition" in self.definitions:
            self.definitions["acquisition"] = replace(self.definitions["acquisition"], schema={
                "type": "object", "additionalProperties": False,
                "properties": {"sample_rate": {"type": "integer", "minimum": 1},
                               "channel_number": {"type": "integer", "minimum": 1},
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
    assert result["CONFIG.revision"] == runtime["CONFIG.revision"]
    assert result["CONFIG.generation"] == runtime["CONFIG.generation"]
    assert result["CONFIG.node_role"] == role
    assert result["ACQUISITION.sample_rate"] == 1000
    assert result["ACQUISITION.channel_number"] == 2
    assert result["ACQUISITION.channel_names"] == ["C3", "C4"]
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
    assert result["CONFIG.revision"] == runtime["CONFIG.revision"]
    assert result["CONFIG.generation"] == runtime["CONFIG.generation"]


def test_stimulation_node_rejects_an_explicit_unavailable_trigger(runtime):
    catalog = LocalCatalog({"paradigm", "trigger"})
    catalog.definitions["trigger"] = replace(catalog.definitions["trigger"], available=False)
    with pytest.raises(PluginSchemaError, match="stimulation.trigger: unavailable"):
        materialize_node_configuration(project_node_configuration(runtime, "stimulation"), catalog)


def test_node_derives_historical_identity_input_without_changing_authoritative_binding(runtime):
    from omnibci.application.processing_source import requires_signal_preparation
    from omnibci.domain.decoder_input import DecoderChannelRequirements
    catalog = LocalCatalog({"acquisition", "decoder", "epoch_detector"})
    catalog.definitions["decoder"] = replace(catalog.definitions["decoder"],
        channel_requirements=DecoderChannelRequirements(default_names=("Oz",)))
    configuration = project_node_configuration(runtime, "engine")
    original = configuration.to_dict()
    result = materialize_node_configuration(configuration, catalog)
    assert selected_processing_input(result) == ProcessingInput()
    assert requires_signal_preparation(result)
    assert result["CONFIG.revision"] == configuration.revision
    assert configuration.to_dict() == original
    assert configuration.bindings[1].processing_input is None

    legacy = {"source_id": "local_acquisition", "outputs": ["C3"]}
    with pytest.raises(PluginSchemaError, match="unsupported fields"):
        NodeConfiguration.from_dict({**original, "settings": {**original["settings"], "SIGNAL.setup": legacy}})



def test_engine_node_projection_ignores_retired_acquisition_montage(runtime):
    source = ProfileBinding.from_dict(runtime["PLUGINS.bindings"][0])
    manual = SignalInfo("local_acquisition", 1000, (ChannelDescriptor(0, "C3", unit="uV"),), 1)
    runtime["PLUGINS.bindings"][0] = replace(source, legacy={"manual_montage": manual.to_dict()}).to_dict()
    dto = NodeConfiguration.from_dict(project_node_configuration(runtime, "engine").to_dict())
    result = materialize_node_configuration(dto, LocalCatalog({"acquisition", "decoder", "epoch_detector"}))
    assert "ACQUISITION.manual_montage" not in result
    assert "manual_montage" not in result["PLUGINS.bindings"][0]
    assert "ACQUISITION.manual_montage" not in project_node_configuration(runtime, "stimulation").settings


def test_projection_requires_no_installed_catalog_and_preserves_parent_identity(runtime):
    dto = project_node_configuration(runtime, "stimulation")
    assert dto.role == "stimulation"
    assert dto.revision == runtime["CONFIG.revision"]
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
    dto = project_node_configuration(runtime, "stimulation")
    expected = dto.to_dict()
    runtime["ACQUISITION.channel_names"].clear()
    exported = dto.to_dict()
    exported["settings"]["ACQUISITION.channel_names"].clear()
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
def test_invalid_sampling_context_rejected_without_source_package(runtime, value):
    runtime["ACQUISITION.sample_rate"] = value
    runtime["PLUGINS.bindings"][0]["parameters"]["sample_rate"] = value
    with pytest.raises(PluginSchemaError, match="sample_rate"):
        project_node_configuration(runtime, "stimulation")


def test_flat_context_cannot_disagree_with_pinned_source(runtime):
    runtime["ACQUISITION.sample_rate"] = 250
    with pytest.raises(PluginSchemaError, match="sample_rate"):
        project_node_configuration(runtime, "stimulation")


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
        data["revision"] = "not-a-revision"
    elif change == "generation":
        data["generation"] = ""
    elif change == "role":
        data["role"] = "arbitrary"
    elif change == "context":
        data["settings"]["ACQUISITION.channel_number"] = True
    elif change == "missing_binding":
        data["bindings"].clear()
    elif change == "duplicate_binding":
        data["bindings"].append(data["bindings"][0])
    with pytest.raises((PluginSchemaError, ValueError)):
        NodeConfiguration.from_dict(data)


def test_disabled_actuation_cannot_be_projected(runtime):
    runtime["ACTUATION.enabled"] = False
    with pytest.raises(PluginSchemaError, match="disabled"):
        project_node_configuration(runtime, "actuation")


def test_source_metadata_can_be_derived_without_flat_aliases(runtime):
    runtime.pop("ACQUISITION.sample_rate")
    runtime.pop("ACQUISITION.channel_number")
    runtime.pop("ACQUISITION.channel_names")
    dto = project_node_configuration(runtime, "stimulation")
    assert dto.settings["ACQUISITION.sample_rate"] == 1000
    assert dto.settings["ACQUISITION.channel_number"] == 2
    assert "ACQUISITION.channel_names" not in dto.settings


@pytest.mark.parametrize(("role", "kinds"), [
    ("engine", {"acquisition", "decoder", "epoch_detector"}),
    ("stimulation", {"paradigm", "trigger"}),
    ("actuation", {"effector"}),
])
def test_nodes_can_start_before_source_expectations_are_confirmed(runtime, role, kinds):
    for name in ("sample_rate", "channel_number"):
        runtime.pop(f"ACQUISITION.{name}")
        runtime["PLUGINS.bindings"][0]["parameters"].pop(name)
    dto = NodeConfiguration.from_dict(project_node_configuration(runtime, role).to_dict())
    result = materialize_node_configuration(dto, LocalCatalog(kinds))
    assert "ACQUISITION.sample_rate" not in dto.settings
    assert "ACQUISITION.channel_number" not in dto.settings
    assert "ACQUISITION.sample_rate" not in result
    assert "ACQUISITION.channel_number" not in result
    assert result["CONFIG.revision"] == runtime["CONFIG.revision"]


def test_nonowned_plugin_parameters_never_receive_local_schema_validation(runtime):
    runtime["PLUGINS.bindings"][1]["parameters"] = {"foreign_only_parameter": [1, 2, 3]}
    result = materialize_node_configuration(
        project_node_configuration(runtime, "actuation"), LocalCatalog({"effector"}),
    )
    assert [item["kind"] for item in result["PLUGINS.bindings"]] == ["effector"]


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
    ("ACQUISITION.channel_names", [123]),
])
def test_node_transport_and_timing_settings_are_validated(runtime, key, value):
    data = project_node_configuration(runtime, "actuation").to_dict()
    data["settings"][key] = value
    with pytest.raises(PluginSchemaError):
        NodeConfiguration.from_dict(data)


def test_legacy_global_setup_cannot_reach_an_engine_node(runtime):
    runtime["SIGNAL.setup"] = {"source_id": "test-source", "outputs": ["O1"]}
    with pytest.raises(ValueError, match="reconfigure"):
        project_node_configuration(runtime, "engine")
    assert "SIGNAL.setup" not in project_node_configuration(runtime, "stimulation").settings
