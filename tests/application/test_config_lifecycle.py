"""Configuration writes and engine lifecycle transitions share an exclusion gate."""
import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.configuration import ConfigProfileConflictError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.shared.events import EventType
from tests.application.test_module_config_service import module_service, create_scan


@pytest.mark.asyncio
async def test_first_start_uses_selection_made_after_api_startup(module_service):
    captured = []
    engine = MagicMock()

    def build():
        captured.append(module_service.resolve_snapshot())
        return engine

    svc = NeuroEngineService(engine_factory=build)
    await svc.startup()
    handler = MagicMock()
    svc.subscribe(EventType.TrialCompleted, handler)
    assert captured == []
    profile = create_scan(module_service)
    module_service.select_module_profile("acquisition", profile.profile_id)
    with patch("omnibci.application.services.engine_service.Thread") as thread:
        thread.return_value.is_alive.return_value = False
        await svc.handle_start_engine(StartEngineCommand())
        assert captured[0]["ACQUISITION.sample_rate"] == 500
        engine.event_bus.register_event_listener.assert_called_once_with(EventType.TrialCompleted, handler)
        await svc.handle_stop_engine(StopEngineCommand())
        module_service.select_module_profile("acquisition", None)
        await svc.handle_start_engine(StartEngineCommand())
    assert captured[1]["ACQUISITION.sample_rate"] == 1000


@pytest.mark.asyncio
async def test_transition_blocks_active_changes_but_allows_inactive_saves(module_service):
    entered, release = threading.Event(), threading.Event()
    engine = MagicMock()

    def ready(_timeout):
        entered.set()
        assert release.wait(3)

    engine.wait_until_started.side_effect = ready
    svc = NeuroEngineService(engine_factory=lambda: engine)
    module_service._change_guard = svc.configuration_change
    await svc.startup()
    active = create_scan(module_service)
    module_service.select_module_profile("acquisition", active.profile_id)
    task = asyncio.create_task(svc.handle_start_engine(StartEngineCommand()))
    assert await asyncio.to_thread(entered.wait, 3)
    try:
        inactive = create_scan(module_service, "Spare")
        module_service.update_module_profile("acquisition", inactive.profile_id, "Spare", {
            **inactive.parameters, "sample_rate": 250,
        })
        with pytest.raises(ConfigProfileConflictError):
            module_service.select_module_profile("acquisition", None)
        with pytest.raises(ConfigProfileConflictError):
            module_service.update_module_profile("acquisition", active.profile_id, "Lab A", {
                **active.parameters, "sample_rate": 250,
            })
        with pytest.raises(ConfigProfileConflictError):
            module_service.apply({"ACQUISITION.sample_rate": 250})
        assert module_service.module_profile("acquisition", active.profile_id) == active
    finally:
        release.set()
        await task
        await svc.handle_stop_engine(StopEngineCommand())


@pytest.mark.asyncio
async def test_bad_selection_prevents_any_engine_thread_and_is_recoverable(module_service):
    profile = create_scan(module_service)
    module_service.select_module_profile("acquisition", profile.profile_id)
    module_service._repository.delete("acquisition", profile.profile_id)

    def build():
        module_service.resolve_snapshot()
        return MagicMock()

    svc = NeuroEngineService(engine_factory=build)
    await svc.startup()
    with patch("omnibci.application.services.engine_service.Thread") as thread:
        with pytest.raises(KeyError):
            await svc.handle_start_engine(StartEngineCommand())
        thread.assert_not_called()
        module_service.select_module_profile("acquisition", None)
        await svc.handle_start_engine(StartEngineCommand())


def test_legacy_full_replacement_cannot_remove_acquisition_parameters_while_running(module_service):
    module_service.put_profile("legacy", {"ACQUISITION.sample_rate": 250})
    module_service.activate("legacy")
    engine = NeuroEngineService(engine_factory=MagicMock())
    module_service._change_guard = engine.configuration_change
    engine._running = True
    with pytest.raises(ConfigProfileConflictError):
        module_service.put_profile("legacy", {"ALGORITHM.window_duration": 4.0})
    assert module_service.resolve_snapshot()["ACQUISITION.sample_rate"] == 250


@pytest.mark.asyncio
async def test_bootstrap_shares_profile_selection_between_status_and_engine(module_service, monkeypatch):
    import omnibci.conf as conf
    from omnibci import bootstrap

    monkeypatch.setattr(conf, "settings", module_service._settings)
    with patch.object(bootstrap, "NeuroEngineBuilder") as builder:
        container = await bootstrap.build_container()
        builder.assert_not_called()
        profile = create_scan(container.config_service)
        container.config_service.select_module_profile("acquisition", profile.profile_id)
        assert container.acquisition_service._device_metadata() == ("neuroscan_scan45_reader", 500, 10)
        with patch("omnibci.application.services.engine_service.Thread"):
            await container.engine_service.handle_start_engine(StartEngineCommand())
        snapshot = builder.call_args.kwargs["settings_snapshot"]
        assert snapshot["ACQUISITION.sample_rate"] == 500
        assert snapshot["ACQUISITION.DATA_PROVIDER_PORT"] == 4000
