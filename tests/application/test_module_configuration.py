"""Each configuration page owns only its module's selections and validation."""
from omnibci.application.activation_settings import section_settings
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ModuleActivation, ProfileBinding, ProfileRevisionConflict
from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugins.accepted_configuration import accepted_runtime_settings
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    return PluginProfileService(
        TomlPluginProfileRepository(tmp_path / "config"), CatalogConfigurationAdapter(),
        settings_source=lambda: {},
    )


def definition(profiles, kind, alias):
    return next(item for item in profiles.catalog.list(kind) if item.plugin_type == alias)


def source_request(profiles):
    plugin = definition(profiles, "acquisition", "neuroscan_scan45_reader")
    return {"slot": "acquisition.source", "selection": plugin.selection.to_dict(),
            "parameters": {"DATA_PROVIDER_IP": "127.0.0.1", "DATA_PROVIDER_PORT": 4000,
                           "sample_rate": 1000, "channel_count": 65}}


def stopped_runtime(profiles):
    engine = NeuroEngineService(lambda _: pytest.fail("Selection must not start an engine"))
    return RuntimeConfigurationService(profiles, engine)


def accept_plan(profiles, runtime, plan):
    stored = runtime.resolve_plan(plan.plan_id)
    if stored.system:
        profiles.repository.save_system(stored.system, expected_revision=plan.expected_revisions["system"])
    for module, candidate in stored.candidates.items():
        profiles.repository.save_module(candidate, expected_revision=plan.expected_revisions[module])


def test_acquisition_can_be_selected_and_started_without_other_modules(profiles):
    runtime = stopped_runtime(profiles)
    plan = runtime.plan("acquisition", [source_request(profiles)], expected_revision=None, settings={})
    assert not plan.errors
    assert plan.signal_summary is None
    assert set(plan.target_revisions) == {"acquisition", "system"}
    accept_plan(profiles, runtime, plan)
    snapshot = accepted_runtime_settings(profiles=profiles)
    assert [item["slot"] for item in snapshot["PLUGINS.bindings"]] == ["acquisition.source"]
    assert snapshot["ACQUISITION.signal_source"] == "neuroscan_scan45_reader"


@pytest.mark.asyncio
async def test_first_acquisition_apply_executes_without_preparing_an_experiment(profiles):
    runtime = stopped_runtime(profiles)
    acquisition, commands = Mock(), Mock()
    runtime.executor = RuntimeApplyCoordinator(
        runtime.engine, profiles, acquisition,
        SimpleNamespace(is_active=False, process_alive=False), commands,
    )
    plan = runtime.plan("acquisition", [source_request(profiles)], expected_revision=None, settings={})
    operation = runtime.submit(plan.plan_id, str(uuid4()))
    await runtime.wait_for_idle()
    assert runtime.operation(operation["operation_id"])["status"] == "succeeded"
    assert [b.slot for b in profiles.repository.get_module("acquisition").bindings] == ["acquisition.source"]
    assert not acquisition.mock_calls and not commands.mock_calls
    assert profiles.repository.get_system() is not None
    assert profiles.repository.list_module_history("acquisition")[0].entry_id == operation["operation_id"]


@pytest.mark.parametrize("problem", ["missing_package", "invalid_parameters"])
def test_acquisition_does_not_inspect_or_modify_dormant_decoder(profiles, monkeypatch, problem):
    plugin = definition(profiles, "decoder", "THUBCI_SSVEP_classic40CCA")
    decoder = ProfileBinding("processing.decoder", "decoder", None, plugin.selection,
                             {"window_duration": 1.0, "offset_time": 0.0}, processing_input=ProcessingInput())
    if problem == "missing_package":
        decoder = replace(decoder, selection=replace(decoder.selection, version="99.0.0"))
    else:
        decoder = replace(decoder, parameters={"window_duration": "invalid"})
    previous = profiles.repository.save_module(ModuleActivation("processing", {}, (decoder,)))
    get = profiles.catalog.get

    def only_acquisition(kind, selection):
        assert kind == "acquisition", "Acquisition must not resolve another module's plugin"
        return get(kind, selection)

    monkeypatch.setattr(profiles.catalog, "get", only_acquisition)
    runtime = stopped_runtime(profiles)
    plan = runtime.plan("acquisition", [source_request(profiles)], expected_revision=None, settings={})
    assert not plan.errors
    assert plan.changed_slots == ("acquisition.source",)
    accept_plan(profiles, runtime, plan)
    assert profiles.repository.get_module("processing") == previous
    snapshot = accepted_runtime_settings(profiles=profiles)
    assert [item["slot"] for item in snapshot["PLUGINS.bindings"]] == ["acquisition.source"]
    assert snapshot["CONFIG.module_revisions"]["processing"] is None


@pytest.mark.parametrize("module,slot,kind,alias", [
    ("processing", "processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
    ("stimulation", "stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
    ("actuation", "actuation.effector", "effector", "logger"),
])
def test_each_module_can_be_configured_first(profiles, module, slot, kind, alias):
    plugin = definition(profiles, kind, alias)
    profile = profiles.create(kind, plugin.selection, alias, {})
    runtime = stopped_runtime(profiles)
    plan = runtime.plan(module, [{"slot": slot, "profile_id": profile.profile_id}], expected_revision=None, settings={})
    assert not plan.errors
    accept_plan(profiles, runtime, plan)
    assert [b.slot for b in profiles.repository.get_module(module).bindings] == [slot]
    assert all(value is None for key, value in profiles.repository.get_activation_set().modules.items() if key != module)


def test_module_request_cannot_change_another_modules_slots(profiles):
    with pytest.raises(PluginSchemaError, match="module"):
        profiles.propose("processing", [source_request(profiles)], settings=section_settings(profiles.settings_source(), "processing"))


def test_acquisition_still_validates_its_own_parameters(profiles):
    request = source_request(profiles)
    request["parameters"]["DATA_PROVIDER_PORT"] = "invalid"
    with pytest.raises(PluginSchemaError):
        stopped_runtime(profiles).plan("acquisition", [request], expected_revision=None, settings={})


def test_unrelated_module_write_does_not_expire_reviewed_plan(profiles):
    profiles.repository.save_system(profiles.propose_system(), expected_revision=None)
    runtime = stopped_runtime(profiles)
    plan = runtime.plan("acquisition", [source_request(profiles)], expected_revision=None, settings={})
    profiles.repository.save_module(profiles.propose("stimulation", [], settings=section_settings(profiles.settings_source(), "stimulation")), expected_revision=None)
    assert runtime.resolve_plan(plan.plan_id).candidates["acquisition"].bindings
    with pytest.raises(ProfileRevisionConflict):
        runtime.plan("stimulation", [], expected_revision=None, settings={})


def test_existing_system_keeps_single_module_target_scoped(profiles):
    profiles.repository.save_system(profiles.propose_system(), expected_revision=None)
    runtime = stopped_runtime(profiles)
    plan = runtime.plan("stimulation", [], expected_revision=None, settings={})
    assert set(plan.target_revisions) == {"stimulation"}
