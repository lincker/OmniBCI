"""Configuration consumers use declared queries and explicit mutation ownership."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.configuration.models import ActivationSet
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.node_configuration_service import NodeConfigurationService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.bootstrap import _RuntimeSettings
from omnibci.application.configuration.accepted import accepted_runtime_settings
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from tests.plugin_configuration_helpers import accept_profiles


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    service = ConfigurationService(TomlConfigurationRepository(tmp_path / "configuration"),
                                   CatalogConfigurationAdapter())
    accept_profiles(service, [
        ("acquisition.source", "acquisition", "file_reader", {}),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("actuation.effector", "effector", "logger", {}),
    ], module_settings={"actuation": {"ACTUATION.auto_spawn": False}})
    return service


def queries(service):
    # Model only the declared consumer surface, with no repository or catalog.
    names = ("get_module", "get_engine", "get_profile", "module_history_entry", "materialize",
             "validate_module", "propose", "propose_engine", "default_module_settings")
    return SimpleNamespace(**{name: getattr(service, name) for name in names},
                           selected_configuration=lambda modules: ActivationSet(
                               {module: service.get_module(module) for module in modules}, service.get_engine()),
                           get_plugin_configuration=service.catalog.get)


def test_selected_query_does_not_read_unrelated_corrupt_module(profiles):
    path = profiles.repository.root / "activation" / "processing.toml"
    path.write_text('revision = "unterminated', encoding="utf-8")
    selected = profiles.selected_configuration(("actuation",))
    assert selected.modules["actuation"] == profiles.get_module("actuation")
    assert selected.modules["processing"] is None
    assert selected.engine == profiles.get_engine()


def test_external_node_selected_projection_uses_queries_only(profiles):
    engine = SimpleNamespace(node_configuration_snapshot=lambda selected, *, module: ("selected", selected()))
    value = NodeConfigurationService(engine, queries(profiles)).configuration("actuation")
    assert value["configuration"]["module_revisions"] == {"actuation": profiles.get_module("actuation").revision}
    assert {binding["kind"] for binding in value["configuration"]["bindings"]} == {"effector"}


def test_startup_selection_uses_queries_without_repository_access(profiles):
    value = accepted_runtime_settings(profiles=queries(profiles), active_kinds={"effector"})
    assert value["CONFIG.module_revisions"]["actuation"] == profiles.get_module("actuation").revision
    assert value["CONFIG.module_revisions"]["acquisition"] is None


def test_idle_status_reads_configuration_queries(profiles):
    from omnibci.application.processing_source import source_id_for

    engine = SimpleNamespace(applied_snapshot=None, is_engine_ready=False)
    view = _RuntimeSettings(engine, queries(profiles))
    assert source_id_for(view) == "OmniBCI.file_reader/default@0.1.0"


def test_planner_uses_queries_for_revision_and_plugin_apply_hints(profiles):
    selected = profiles.selected_configuration()
    engine = NeuroEngineService(lambda _: pytest.fail("Planning must not start the engine"))
    engine._running = engine._accepting_commands = True
    engine._engine = SimpleNamespace()
    engine._engine_thread = SimpleNamespace(is_alive=lambda: True)
    engine._applied_snapshot = profiles.materialize(selected, "generation")
    engine.is_module_running = lambda module: True
    engine.is_peer_connected = lambda role: False
    service = RuntimeConfigurationService(queries(profiles), engine)
    current = profiles.get_module("processing")
    decoder = next(binding for binding in current.bindings if binding.kind == "decoder")
    profile = profiles.get_profile(decoder.profile_id)
    profiles.update(profile.profile_id, name=profile.name,
                    parameters={**profile.parameters, "window_duration": 1.5}, expected_revision=profile.revision)
    plan = service.plan("processing", [{"slot": binding.slot, "profile_id": binding.profile_id}
                                      for binding in current.bindings],
                        expected_revision=current.revision, settings=current.settings)
    assert not plan.errors and plan.strategy == "trial_boundary"
    assert service.resolve_plan(plan.plan_id).plan == plan
    assert service.status()["accepted"]["processing"] == current.revision


def test_plan_rejects_unknown_strategy_before_submission(profiles):
    service = RuntimeConfigurationService(profiles, NeuroEngineService(lambda _: None))
    plan = service.plan("actuation", [], expected_revision=profiles.get_module("actuation").revision,
                        settings=profiles.default_module_settings("actuation"))
    with pytest.raises(ValueError, match="strategy|ApplyStrategy"):
        replace(plan, strategy="best_effort")
    assert plan.to_dict()["strategy"] == "select_on_start"


@pytest.mark.asyncio
@pytest.mark.parametrize("history_failure", [False, True])
async def test_apply_owns_explicit_repository_without_exposing_it_to_queries(profiles, monkeypatch, history_failure):
    engine = NeuroEngineService(lambda _: pytest.fail("Selection must not start the engine"))
    surface = queries(profiles)
    coordinator = RuntimeApplyCoordinator(engine, surface, None,
        SimpleNamespace(is_active=False, process_alive=False), None, repository=profiles.repository)
    service = RuntimeConfigurationService(surface, engine, executor=coordinator)
    current = profiles.get_module("actuation")
    plan = service.plan("actuation", [], expected_revision=current.revision,
                        settings=profiles.default_module_settings("actuation"))
    if history_failure:
        def fail(*args, **kwargs):
            raise OSError("history disk unavailable")
        monkeypatch.setattr(profiles.repository, "record_module_history", fail)
    operation = service.submit(plan.plan_id, str(uuid4()))
    await service.wait_for_idle()
    result = service.operation(operation["operation_id"])
    assert result["status"] == "succeeded"
    assert result["result"]["outcome"] == "selected"
    assert result["result"]["applied"] is True
    assert result["result"]["history_saved"] is not history_failure
    assert profiles.get_module("actuation").revision == plan.target_revisions["actuation"]
    if history_failure:
        assert "history disk unavailable" in result["result"]["warning"]
    else:
        assert profiles.module_history("actuation")[0].entry_id == operation["operation_id"]


def test_signal_setup_uses_queries_for_selected_profiles(profiles):
    from omnibci.application.services.signal_setup_service import SignalSetupService

    engine = SimpleNamespace(is_engine_ready=False, applied_snapshot=None, admission=None,
                             is_module_running=lambda module: False)
    acquisition = SimpleNamespace(cached_descriptor_for=lambda binding: None)
    service = SignalSetupService(queries(profiles), engine, acquisition, None)
    state = service.read()
    assert state["selected_source_id"] == "OmniBCI.file_reader/default@0.1.0"
    assert not state["readiness"]["ready"]


def test_returned_plan_revision_edit_does_not_change_the_saved_review(profiles):
    service = RuntimeConfigurationService(profiles, NeuroEngineService(lambda _: None))
    previous = profiles.get_module("actuation").revision
    plan = service.plan("actuation", [], expected_revision=previous,
                        settings=profiles.default_module_settings("actuation"))
    plan.expected_revisions["actuation"] = "caller-edit"
    stored = service.resolve_plan(plan.plan_id)
    assert stored.plan.expected_revisions["actuation"] == previous


def test_resolved_candidate_edit_cannot_replace_the_pinned_configuration(profiles):
    service = RuntimeConfigurationService(profiles, NeuroEngineService(lambda _: None))
    plan = service.plan("actuation", [], expected_revision=profiles.get_module("actuation").revision,
                        settings=profiles.default_module_settings("actuation"))
    exposed = service.resolve_plan(plan.plan_id)
    exposed.candidates.clear()
    exposed.plan.target_revisions.clear()
    stored = service.resolve_plan(plan.plan_id)
    assert stored.candidates["actuation"].revision == plan.target_revisions["actuation"]
