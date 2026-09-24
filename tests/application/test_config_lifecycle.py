"""Configuration writes and engine lifecycle transitions share an exclusion gate."""
import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.services.config_service import ConfigService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.conf import OmniBCISettings
from omnibci.shared.events import EventType


@pytest.fixture
def application_config(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[ACQUISITION]\nsignal_source = "file_reader"\nsample_rate = 1000\n'
                    'channel_number = 10\ndata_file_path = "recordings/demo.cnt"\n'
                    'file_reader_chunk_size = 100\ntrigger = ""\n'
                    '[ALGORITHM]\ndecoder = "THUBCI_SSVEP_classic40FBCCA"\n'
                    'epoch_detector = "DEFAULT_EPOCH_DETECTOR"\nfilters = []\n'
                    '[STIMULATION]\nparadigm = "STIMULATION_THUBCI_SSVEP_classic40"\n'
                    'auto_spawn = false\n[ACTUATION]\neffector = ""\nenabled = false\n',
                    encoding="utf-8")
    return ConfigService(OmniBCISettings(path))


@pytest.mark.asyncio
async def test_first_start_uses_application_profile_selected_after_api_startup(application_config):
    captured = []
    engine = MagicMock()

    def build(snapshot):
        captured.append(snapshot)
        return engine

    svc = NeuroEngineService(engine_factory=build, snapshot_provider=application_config._settings.snapshot)
    await svc.startup()
    handler = MagicMock()
    svc.subscribe(EventType.TrialCompleted, handler)
    assert captured == []
    application_config.put_profile("Lab", {"ACQUISITION.sample_rate": 500})
    application_config.activate("Lab")
    with patch("omnibci.application.services.engine_service.Thread") as thread:
        thread.return_value.is_alive.return_value = False
        await svc.handle_start_engine(StartEngineCommand())
        assert captured[0]["ACQUISITION.sample_rate"] == 500
        engine.event_bus.register_event_listener.assert_called_once_with(EventType.TrialCompleted, handler)
        await svc.handle_stop_engine(StopEngineCommand())
        application_config.deactivate()
        await svc.handle_start_engine(StartEngineCommand())
    assert captured[1]["ACQUISITION.sample_rate"] == 1000


@pytest.mark.asyncio
async def test_transition_blocks_active_changes_but_allows_inactive_saves(application_config):
    entered, release = threading.Event(), threading.Event()
    engine = MagicMock()

    def ready(_timeout):
        entered.set()
        assert release.wait(3)

    engine.wait_until_started.side_effect = ready
    svc = NeuroEngineService(engine_factory=lambda: engine)
    application_config._change_guard = svc.configuration_change
    await svc.startup()
    application_config.put_profile("Lab", {"ACQUISITION.sample_rate": 500})
    application_config.activate("Lab")
    task = asyncio.create_task(svc.handle_start_engine(StartEngineCommand()))
    assert await asyncio.to_thread(entered.wait, 3)
    try:
        application_config.put_profile("Spare", {"ACQUISITION.sample_rate": 250})
        with pytest.raises(ConfigProfileConflictError):
            application_config.deactivate()
        with pytest.raises(ConfigProfileConflictError):
            application_config.apply({"ACQUISITION.sample_rate": 250}, scope="Lab")
        with pytest.raises(ConfigProfileConflictError):
            application_config.apply({"ACQUISITION.sample_rate": 250})
        assert application_config.profile_detail("Lab")["entries"] == {"ACQUISITION.sample_rate": 500}
        assert application_config.profile_detail("Spare")["entries"] == {"ACQUISITION.sample_rate": 250}
    finally:
        release.set()
        await task
        await svc.handle_stop_engine(StopEngineCommand())


def test_application_profile_replacement_cannot_remove_acquisition_parameters_while_running(application_config):
    application_config.put_profile("Lab", {"ACQUISITION.sample_rate": 250})
    application_config.activate("Lab")
    engine = NeuroEngineService(engine_factory=MagicMock())
    application_config._change_guard = engine.configuration_change
    engine._running = True
    with pytest.raises(ConfigProfileConflictError):
        application_config.put_profile("Lab", {"ALGORITHM.window_duration": 4.0})
    assert application_config._settings.snapshot()["ACQUISITION.sample_rate"] == 250


@pytest.mark.asyncio
async def test_bootstrap_shares_pinned_plugin_selection_between_status_and_engine(application_config, monkeypatch, tmp_path):
    import omnibci.conf as conf
    from omnibci import bootstrap

    monkeypatch.setattr(conf, "settings", application_config._settings)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    with patch.object(bootstrap, "NeuroEngineBuilder") as builder:
        container = await bootstrap.build_container()
        builder.assert_not_called()
        profiles = container.plugin_profile_service
        selected = profiles.create("acquisition", profiles.catalog.legacy(
            "acquisition", "neuroscan_scan45_reader").selection, "Lab", {
                "DATA_PROVIDER_IP": "192.168.0.3", "DATA_PROVIDER_PORT": 4000,
                "sample_rate": 500, "channel_number": 10,
            })
        bindings = [{"slot": "acquisition.source", "profile_id": selected.profile_id}]
        for slot, kind, alias in (
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
            ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
        ):
            selected = profiles.create(kind, profiles.catalog.legacy(kind, alias).selection, alias, {})
            bindings.append({"slot": slot, "profile_id": selected.profile_id})
        accepted = profiles.repository.get_activation()
        profiles.accept(profiles.propose(bindings), expected_revision=accepted.revision)
        assert container.acquisition_service._device_metadata() == ("neuroscan_scan45_reader", 500, 10)
        with patch("omnibci.application.services.engine_service.Thread"):
            await container.engine_service.handle_start_engine(StartEngineCommand())
        snapshot = builder.call_args.kwargs["settings_snapshot"]
        assert snapshot["ACQUISITION.sample_rate"] == 500
        assert snapshot["ACQUISITION.DATA_PROVIDER_PORT"] == 4000
