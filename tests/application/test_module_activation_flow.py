"""Module application records successful operations without coupling other sections."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.infrastructure.plugin_management.catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


@pytest.fixture
def flow(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    profiles = ConfigurationService(TomlConfigurationRepository(tmp_path / "config"),
                                    CatalogConfigurationAdapter())
    engine = NeuroEngineService(lambda _: pytest.fail("Selecting configuration must not start workers"))
    executor = RuntimeApplyCoordinator(engine, profiles, None,
                                      SimpleNamespace(is_active=False, process_alive=False), None, repository=profiles.repository)
    runtime = RuntimeConfigurationService(profiles, engine, executor=executor)
    from tests.plugin_configuration_helpers import builtin_selection
    definition = profiles.catalog.get("acquisition", builtin_selection("acquisition", "file_reader"))
    bindings = [{"slot": "acquisition.source", "selection": definition.selection.to_dict(),
                 "parameters": {"file_reader_chunk_size": 100}}]
    return SimpleNamespace(profiles=profiles, runtime=runtime, bindings=bindings)


async def execute(flow, plan, operation_id=None):
    assert not plan.errors
    operation = flow.runtime.submit(plan.plan_id, operation_id or str(uuid4()))
    await flow.runtime.wait_for_idle()
    return flow.runtime.operation(operation["operation_id"])


def source_plan(flow):
    current = flow.profiles.repository.get_module("acquisition")
    return flow.runtime.plan("acquisition", flow.bindings, settings={},
                             expected_revision=current.revision if current else None)


@pytest.mark.asyncio
async def test_first_selection_initializes_engine_and_never_creates_saved_profile(flow):
    plan = source_plan(flow)
    assert plan.expected_revision is None
    assert set(plan.target_revisions) == {"acquisition", "engine"}
    operation = await execute(flow, plan)
    assert operation["status"] == "succeeded", operation
    status = flow.runtime.status()
    assert status["accepted"]["acquisition"] == plan.target_revisions["acquisition"]
    assert status["accepted"]["engine"] == plan.target_revisions["engine"]
    assert status["accepted"]["processing"] is None
    assert status["applied"] is None
    assert flow.profiles.repository.list_profiles() == []


@pytest.mark.asyncio
async def test_restore_records_a_b_a_without_deduplicating_equal_content(flow):
    first = await execute(flow, source_plan(flow))
    flow.bindings[0]["parameters"]["file_reader_chunk_size"] = 200
    second = await execute(flow, source_plan(flow))
    restore = flow.runtime.revert_plan("acquisition", first["operation_id"])
    third = await execute(flow, restore)
    assert third["status"] == "succeeded", third
    history = flow.profiles.repository.list_module_history("acquisition")
    assert [entry.entry_id for entry in history] == [third["operation_id"], second["operation_id"], first["operation_id"]]
    assert history[0].revision == history[2].revision != history[1].revision
    assert history[0].restored_from == first["operation_id"]
    assert history[0].activation.bindings[0].parameters["file_reader_chunk_size"] == 100
    assert flow.profiles.repository.get_module("acquisition").revision == history[0].revision


@pytest.mark.asyncio
async def test_retry_and_no_change_do_not_duplicate_history(flow):
    plan = source_plan(flow)
    first = await execute(flow, plan)
    repeated = await execute(flow, plan, first["operation_id"])
    assert repeated == first
    unchanged = source_plan(flow)
    assert unchanged.strategy == "no_change"
    assert (await execute(flow, unchanged))["status"] == "succeeded"
    assert len(flow.profiles.repository.list_module_history("acquisition")) == 1


@pytest.mark.asyncio
async def test_unrelated_module_write_does_not_expire_a_plan(flow):
    assert (await execute(flow, source_plan(flow)))["status"] == "succeeded"
    flow.bindings[0]["parameters"]["file_reader_chunk_size"] = 200
    plan = source_plan(flow)
    flow.profiles.repository.save_module(flow.profiles.propose("stimulation", [], settings=flow.profiles.default_module_settings("stimulation")), expected_revision=None)
    assert flow.runtime.resolve_plan(plan.plan_id).plan == plan
    assert (await execute(flow, plan))["status"] == "succeeded"
    assert flow.profiles.repository.get_module("stimulation") is not None
    assert flow.profiles.repository.list_module_history("stimulation") == []


@pytest.mark.asyncio
async def test_failed_first_selection_restores_missing_engine_without_history(flow, monkeypatch):
    plan = source_plan(flow)

    def fail(*args, **kwargs):
        raise OSError("Cannot replace module file")

    monkeypatch.setattr(flow.profiles.repository, "save_module", fail)
    result = await execute(flow, plan)
    assert result["status"] == "failed"
    assert flow.profiles.repository.get_engine() is None
    assert flow.profiles.repository.get_module("acquisition") is None
    assert flow.profiles.repository.list_module_history("acquisition") == []


@pytest.mark.asyncio
async def test_history_write_failure_reports_applied_values_without_quarantining_runtime(flow, monkeypatch):
    plan = source_plan(flow)

    def fail(*args, **kwargs):
        raise OSError("History storage unavailable")

    monkeypatch.setattr(flow.profiles.repository, "record_module_history", fail)
    operation = await execute(flow, plan)
    assert operation["status"] == "succeeded"
    assert operation["result"]["applied"] is True
    assert operation["result"]["history_saved"] is False
    assert "History storage unavailable" in operation["result"]["warning"]
    assert flow.profiles.repository.get_module("acquisition").revision == plan.target_revisions["acquisition"]
    assert flow.runtime.status()["recovery"] == {}
    assert flow.profiles.repository.list_module_history("acquisition") == []


@pytest.mark.asyncio
async def test_changed_engine_rejects_pending_module_plan(flow):
    await execute(flow, source_plan(flow))
    plan = source_plan(flow)
    previous = flow.profiles.repository.get_engine()
    changed = flow.profiles.propose_engine({**previous.settings, "debug": True})
    flow.profiles.repository.save_engine(changed, expected_revision=previous.revision)
    with pytest.raises(ProfileRevisionConflict):
        flow.runtime.resolve_plan(plan.plan_id)
