"""A node validates only its pinned capabilities and shared runtime context."""
from dataclasses import replace

import pytest

from omnibci.application.node_configuration import (
    NodeConfiguration, materialize_node_configuration, project_node_configuration,
)
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.processing_source import processing_input as selected_processing_input
from omnibci.application.plugin_management.schema import PluginSchemaError
from omnibci.application.plugin_management.catalog import PluginConfiguration
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
        "CONFIG.module_revisions": {'acquisition': "a" * 64, 'processing': "a" * 64, 'stimulation': "a" * 64, 'actuation': "a" * 64}, "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "engine-generation-1", "CONFIG.operation_id": "prepare-1",
        "ACQUISITION.sample_rate": 1000, "ACQUISITION.channel_count": 2,
        "STIMULATION.server_ip": "192.0.2.1", "STIMULATION.server_port": 8765,
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
                selection=binding(kind).selection, kind=kind, domain=SLOTS[kind].split(".")[0],
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


@pytest.mark.parametrize(("role", "kinds"), [
    ("stimulation", {"paradigm", "trigger"}),
    ("actuation", {"effector"}),
])
def test_node_needs_only_owned_plugins(runtime, role, kinds):
    catalog = LocalCatalog(kinds)
    dto = project_node_configuration(runtime, role)
    result = materialize_node_configuration(NodeConfiguration.from_dict(dto.to_dict()), catalog)

    assert {kind for kind, _ in catalog.calls} == kinds
    assert {item["kind"] for item in result["PLUGINS.bindings"]} == kinds
    assert "ACQUISITION.signal_source" not in result
    assert "STIMULATION.paradigm" not in result
    assert "ACTUATION.effector" not in result
    assert "ALGORITHM.decoder" not in result
    assert "ALGORITHM.epoch_detector" not in result
    assert "ALGORITHM.filters" not in result
    assert "ACQUISITION.trigger" not in result
    assert result["CONFIG.engine_revision"] == runtime["CONFIG.engine_revision"]
    assert result["CONFIG.generation"] == runtime["CONFIG.generation"]
    assert result["CONFIG.node_role"] == role
    assert "ACQUISITION.sample_rate" not in result
    assert "ACQUISITION.channel_count" not in result
    assert "ACQUISITION.channel_names" not in result
    assert "LOGGING.file_path" not in result
    assert "custom.private_key" not in result
    assert "ACQUISITION.data_file_path" not in result
    assert "NEUROENGINE.queue_size" not in result
    if role == "stimulation":
        assert result["STIMULATION.server_ip"] == "192.0.2.1"
        assert result["STIMULATION.server_port"] == 8765
        assert "ACTUATION.server_ip" not in result
    if role == "actuation":
        assert "STIMULATION.renderer" not in result
        assert "STIMULATION.server_ip" not in result


@pytest.mark.parametrize("key,value", [
    ("STIMULATION.server_port", True), ("STIMULATION.server_port", 0),
    ("STIMULATION.server_port", 65536), ("STIMULATION.server_port", "8765"),
    ("STIMULATION.server_ip", ""), ("STIMULATION.server_ip", 127),
])
def test_stimulation_node_transport_settings_are_validated(runtime, key, value):
    data = project_node_configuration(runtime, "stimulation").to_dict()
    data["settings"][key] = value
    with pytest.raises(PluginSchemaError, match="invalid node setting value"):
        NodeConfiguration.from_dict(data)


def test_stimulation_node_preserves_an_explicit_absent_trigger(runtime):
    runtime["PLUGINS.bindings"] = [item for item in runtime["PLUGINS.bindings"] if item["kind"] != "trigger"]
    runtime["ACQUISITION.trigger"] = "stale-hardware-alias"
    catalog = LocalCatalog({"paradigm"})
    dto = project_node_configuration(runtime, "stimulation")
    result = materialize_node_configuration(NodeConfiguration.from_dict(dto.to_dict()), catalog)
    assert {kind for kind, _ in catalog.calls} == {"paradigm"}
    assert {item["slot"] for item in result["PLUGINS.bindings"]} == {"stimulation.paradigm"}
    assert "ACQUISITION.trigger" not in result
    assert result["CONFIG.engine_revision"] == runtime["CONFIG.engine_revision"]
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
    assert dto.engine_revision == runtime["CONFIG.engine_revision"]
    assert dto.generation == runtime["CONFIG.generation"]
    assert dto.to_dict()["format_version"] == 1
    assert dto.settings["STIMULATION.auto_spawn"] is False
    runtime.pop("CONFIG.generation")
    runtime.pop("CONFIG.operation_id")
    assert project_node_configuration(runtime, "stimulation").generation is None






def test_projected_dto_is_independent_of_input_and_export_mutation(runtime):
    dto = project_node_configuration(runtime, "stimulation")
    expected = dto.to_dict()
    exported = dto.to_dict()
    exported["settings"]["STIMULATION.server_ip"] = "127.0.0.1"
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


@pytest.mark.parametrize(("role", "kinds"), [
    ("stimulation", {"paradigm", "trigger"}),
    ("actuation", {"effector"}),
])
def test_output_nodes_never_receive_native_channel_labels(runtime, role, kinds):
    labels = ["C3", "Trigger"]
    runtime["PLUGINS.bindings"][0]["parameters"]["channel_labels"] = labels
    dto = NodeConfiguration.from_dict(project_node_configuration(runtime, role).to_dict())
    result = materialize_node_configuration(dto, LocalCatalog(kinds))
    assert "ACQUISITION.channel_labels" not in dto.settings
    assert "ACQUISITION.channel_labels" not in result


def test_engine_role_is_rejected_as_unsupported(runtime):
    with pytest.raises(PluginSchemaError, match="Unsupported node role"):
        project_node_configuration(runtime, "engine")


@pytest.mark.parametrize("change", ["foreign_binding", "foreign_setting", "unknown_field", "version", "unsupported_version", "legacy_identity", "legacy_identity_extra", "revision", "generation", "role", "context", "missing_binding", "duplicate_binding"])
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
    elif change == "unsupported_version":
        data["format_version"] = 2
    elif change == "legacy_identity":
        data["system_revision"] = data.pop("engine_revision")
    elif change == "legacy_identity_extra":
        data["system_revision"] = data["engine_revision"]
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


@pytest.mark.parametrize(("role", "kinds"), [
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
    assert result["CONFIG.engine_revision"] == runtime["CONFIG.engine_revision"]


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


@pytest.mark.parametrize("operation", [None, "", " ", True])
def test_running_external_node_requires_preparation_operation(runtime, operation):
    runtime["CONFIG.operation_id"] = operation
    with pytest.raises(PluginSchemaError, match="operation"):
        project_node_configuration(runtime, "actuation")


@pytest.fixture
def node_engine():
    from unittest.mock import Mock
    from omnibci.application.services.engine_service import NeuroEngineService

    return NeuroEngineService(engine_factory=Mock())


def _running_node_engine(engine, snapshot):
    from types import SimpleNamespace
    from unittest.mock import Mock

    engine._engine = Mock()
    engine._running = engine._accepting_commands = True
    engine._engine_thread = SimpleNamespace(is_alive=lambda: True)
    engine._applied_snapshot = snapshot


def test_running_publication_uses_applied_snapshot_without_reading_saved_files(node_engine):
    from unittest.mock import Mock

    _running_node_engine(
        node_engine,
        {
            "CONFIG.module_revisions": {"acquisition": "running", "processing": "running", "stimulation": "running", "actuation": "running"},
            "CONFIG.engine_revision": "c" * 64,
            "nested": {"value": 1},
        },
    )
    selected = Mock(side_effect=AssertionError("must not read saved configuration"))
    state, snapshot = node_engine.node_configuration_snapshot(selected)
    assert state == "running"
    assert snapshot["CONFIG.module_revisions"]["acquisition"] == "running"
    snapshot["nested"]["value"] = 9
    assert node_engine._applied_snapshot["nested"]["value"] == 1


def test_idle_publication_ignores_historical_applied_snapshot(node_engine):
    node_engine._applied_snapshot = {
        "CONFIG.module_revisions": {"acquisition": "old", "processing": "old", "stimulation": "old", "actuation": "old"},
        "CONFIG.engine_revision": "c" * 64,
        "CONFIG.generation": "old",
    }
    expected = {
        "CONFIG.module_revisions": {"acquisition": "selected", "processing": "selected", "stimulation": "selected", "actuation": "selected"},
        "CONFIG.engine_revision": "c" * 64,
    }
    assert node_engine.node_configuration_snapshot(lambda: expected) == ("selected", expected)


@pytest.mark.parametrize("phase", ["starting", "stopping", "failed_stop", "dead_thread", "apply", "recovery"])
def test_unsettled_runtime_never_publishes_a_node_configuration(node_engine, phase):
    from unittest.mock import Mock
    from omnibci.application.configuration.errors import ConfigProfileConflictError, ProfileRevisionConflict

    selected = Mock(side_effect=AssertionError("must not read saved configuration"))
    reservation = None
    if phase in ("starting", "stopping"):
        node_engine._transitioning = True
    elif phase == "failed_stop":
        node_engine._restart_blocked = True
    elif phase == "dead_thread":
        node_engine._running = True
    elif phase == "apply":
        reservation = node_engine.admission.reserve_transition()
    else:
        node_engine.admission.quarantine("uncertain cleanup")
    try:
        with pytest.raises((ProfileRevisionConflict, ConfigProfileConflictError)):
            node_engine.node_configuration_snapshot(selected)
    finally:
        if reservation is not None:
            node_engine.admission.release_reservation(reservation)


@pytest.mark.parametrize(("role", "snapshot", "message"), [
    ("engine", {}, "role"),
    ("stimulation", {"STIMULATION.auto_spawn": True}, "managed"),
    ("actuation", {"ACTUATION.auto_spawn": True}, "managed"),
])
def test_node_endpoint_rejects_unowned_roles(role, snapshot, message):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from omnibci.application.configuration.errors import ConfigProfileConflictError
    from omnibci.application.services.node_configuration_service import NodeConfigurationService

    engine = SimpleNamespace(node_configuration_snapshot=lambda selected, *, module: ("running", snapshot))
    with pytest.raises((ValueError, ConfigProfileConflictError), match=message):
        NodeConfigurationService(engine, Mock()).configuration(role)


def test_unrelated_processing_transition_and_recovery_do_not_block_stimulation_publication(node_engine):
    from omnibci.application.configuration.errors import ProfileRevisionConflict

    reservation = node_engine.admission.reserve_transition("processing")
    node_engine.admission.quarantine("Processing acknowledgement missing", "processing")
    try:
        assert node_engine.node_configuration_snapshot(lambda: {"role": "stimulation"}, module="stimulation") == (
            "selected", {"role": "stimulation"})
        with pytest.raises(ProfileRevisionConflict):
            node_engine.node_configuration_snapshot(lambda: {}, module="processing")
    finally:
        node_engine.admission.release_reservation(reservation)


def test_running_node_publication_requires_a_prepared_output_operation(node_engine):
    from unittest.mock import Mock
    from omnibci.application.configuration.errors import ConfigProfileConflictError

    _running_node_engine(node_engine, {"STIMULATION.auto_spawn": False, "CONFIG.generation": "run-1"})
    node_engine._engine.node_operation_id.return_value = None
    with pytest.raises(ConfigProfileConflictError, match="Prepare"):
        node_engine.node_configuration_snapshot(Mock(), module="stimulation")


def test_node_operation_is_read_from_the_current_host_without_mutating_applied_state(node_engine):
    from unittest.mock import Mock

    _running_node_engine(node_engine, {"STIMULATION.auto_spawn": False, "CONFIG.generation": "run-1"})
    node_engine._engine.node_operation_id.return_value = "operation-1"
    state, snapshot = node_engine.node_configuration_snapshot(Mock(), module="stimulation")
    assert state == "running"
    assert snapshot["CONFIG.operation_id"] == "operation-1"
    assert "CONFIG.operation_id" not in node_engine._applied_snapshot
    node_engine._engine.node_operation_id.return_value = "operation-2"
    assert node_engine.node_configuration_snapshot(Mock(), module="stimulation")[1]["CONFIG.operation_id"] == "operation-2"
