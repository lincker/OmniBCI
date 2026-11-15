"""Current plugin values can be applied independently of saved profiles."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter())
    engine = NeuroEngineService(lambda _: pytest.fail("Selecting current values must not start an engine"))
    coordinator = RuntimeApplyCoordinator(engine, profiles, None,
                                          SimpleNamespace(is_active=False, process_alive=False), None)
    return RuntimeConfigurationService(profiles, engine, executor=coordinator)


def draft(runtime, slot, kind, alias, **extra):
    definition = next(item for item in runtime.profiles.catalog.list(kind) if item.plugin_type == alias)
    return {"slot": slot, "selection": definition.selection.to_dict(), "parameters": {},
            "configuration_version": definition.configuration_version,
            **({"processing_input": {"schema_version": 2, "outputs": []}} if kind == "decoder" else {}), **extra}


async def apply_current(runtime, bindings, module):
    previous = runtime.profiles.repository.get_module(module)
    plan = runtime.plan(module, bindings, settings={}, expected_revision=previous.revision if previous else None)
    assert not plan.errors
    operation = runtime.submit(plan.plan_id, str(uuid4()))
    await runtime.wait_for_idle()
    result = runtime.operation(operation["operation_id"])
    assert result["status"] == "succeeded", result
    return runtime.profiles.repository.get_module(module)


@pytest.mark.parametrize("slot,kind,alias", [
    ("acquisition.source", "acquisition", "file_reader"),
    ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
    ("processing.filter.00000000-0000-0000-0000-000000000001", "filter", "BASELINE_CORRECTION"),
    ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("stimulation.trigger", "trigger", "neuroscan_scan45_trigger"),
    ("actuation.effector", "effector", "logger"),
])
@pytest.mark.asyncio
async def test_apply_current_values_never_requires_or_creates_a_profile(runtime, slot, kind, alias):
    current = draft(runtime, slot, kind, alias)
    selected = await apply_current(runtime, [current], slot.split(".")[0])
    binding, = selected.bindings
    assert binding.kind == kind
    assert binding.profile_id is None
    assert binding.profile_revision == 0
    assert binding.selection.to_dict() == current["selection"]
    assert runtime.profiles.repository.list_profiles() == []
    assert runtime.status()["accepted"][slot.split(".")[0]] == selected.revision
    assert runtime.status()["applied"] is None


@pytest.mark.asyncio
async def test_processing_current_values_pin_mapping_and_filter_order_without_changing_source(runtime):
    source = await apply_current(runtime, [draft(runtime, "acquisition.source", "acquisition", "file_reader")],
                                 "acquisition")
    mapping = {"schema_version": 2, "outputs": [
        {"source_channel_index": 2, "name": "Right", "signal_type": "eeg"},
        {"source_channel_index": 0, "name": "Left", "signal_type": "eeg"},
    ]}
    decoder = draft(runtime, "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA",
                    processing_input=mapping)
    filters = [draft(runtime, f"processing.filter.{uuid4()}", "filter", "BASELINE_CORRECTION") for _ in range(2)]
    plan = runtime.plan("processing", [decoder, *filters], settings={}, expected_revision=None)
    decoder["parameters"]["window_duration"] = 8
    mapping["outputs"].reverse()
    candidate = runtime.resolve_plan(plan.plan_id).candidates["processing"]
    assert runtime.profiles.repository.get_module("acquisition") == source
    assert candidate.bindings[0].parameters["window_duration"] != 8
    assert [row.name for row in candidate.bindings[0].processing_input.outputs] == ["Right", "Left"]
    operation = runtime.submit(plan.plan_id, str(uuid4()))
    await runtime.wait_for_idle()
    assert runtime.operation(operation["operation_id"])["status"] == "succeeded"
    updated = await apply_current(runtime, [decoder, *reversed(filters)], "processing")
    assert runtime.profiles.repository.get_module("acquisition") == source
    assert [binding.slot for binding in updated.bindings if binding.kind == "filter"] == [
        item["slot"] for item in reversed(filters)
    ]
    assert [row.name for row in updated.bindings[0].processing_input.outputs] == ["Left", "Right"]
    assert runtime.profiles.repository.list_profiles() == []


@pytest.mark.parametrize("changes", [
    {"slot": "processing.unknown"},
    {"slot": "processing.filter.invalid"},
    {"slot": "processing.epoch_detector"},
    {"kind": "filter"},
    {"configuration_version": 999},
    {"processing_input": None},
    {"processing_input": {"schema_version": 1, "outputs": []}},
    {"processing_input": {"outputs": [{"name": "Missing source"}]}},
    {"processing_input": {"outputs": [
        {"name": "Repeated", "source_channel_index": 0},
        {"name": "Repeated", "source_channel_index": 1},
    ]}},
])
def test_invalid_current_binding_cannot_change_configuration(runtime, changes):
    decoder = draft(runtime, "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA")
    with pytest.raises((ValueError, KeyError)):
        runtime.plan("processing", [{**decoder, **changes}], settings={}, expected_revision=None)
    assert all(item is None for item in runtime.profiles.repository.get_activation_set().revisions().values())
    assert runtime.profiles.repository.list_profiles() == []


def test_current_decoder_requires_an_explicit_mapping(runtime):
    decoder = draft(runtime, "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA")
    decoder.pop("processing_input")
    with pytest.raises(ValueError, match="processing_input"):
        runtime.plan("processing", [decoder], settings={}, expected_revision=None)
    assert all(item is None for item in runtime.profiles.repository.get_activation_set().revisions().values())


def test_non_decoder_current_binding_rejects_processing_input(runtime):
    source = draft(runtime, "acquisition.source", "acquisition", "file_reader",
                   processing_input={"schema_version": 2, "outputs": []})
    with pytest.raises(ValueError, match="processing_input"):
        runtime.plan("acquisition", [source], settings={}, expected_revision=None)
