"""Configuration writes and engine lifecycle transitions share an exclusion gate."""
import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.services.config_service import ConfigService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.conf import OmniBCISettings


@pytest.fixture
def application_config(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[ACQUISITION]\nsignal_source = "file_reader"\nsample_rate = 1000\n'
                    'channel_number = 10\ndata_file_path = "recordings/demo.cnt"\n'
                    'file_reader_chunk_size = 100\ntrigger = ""\n'
                    '[ALGORITHM]\ndecoder = "THUBCI_SSVEP_classic40FBCCA"\n'
                    'epoch_detector = "DEFAULT_EPOCH_DETECTOR"\nfilters = []\nbaseline_duration = 1.0\n'
                    '[NEUROENGINE]\nqueue_size = 32\n'
                    '[STIMULATION]\nparadigm = "STIMULATION_THUBCI_SSVEP_classic40"\n'
                    'auto_spawn = false\n[ACTUATION]\neffector = ""\nenabled = false\n',
                    encoding="utf-8")
    return ConfigService(OmniBCISettings(path))


def test_host_settings_can_be_saved_while_running_without_applying(application_config):
    builder = MagicMock()
    engine = NeuroEngineService(engine_factory=builder)
    application_config._change_guard = engine.configuration_change
    engine._running = True
    engine._applied_snapshot = application_config._settings.snapshot()

    saved, restart, _ = application_config.save(
        {"ALGORITHM.baseline_duration": 2.0}, expected_revision=application_config.read()[1])

    assert saved == ["ALGORITHM.baseline_duration"]
    assert restart is True
    assert application_config._settings.get("ALGORITHM.baseline_duration") == 2.0
    assert engine.applied_snapshot["ALGORITHM.baseline_duration"] == 1.0
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_apply_blocks_host_save_without_partial_write(application_config, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    engine = NeuroEngineService(engine_factory=MagicMock())
    application_config._change_guard = engine.configuration_change
    path = tmp_path / "settings.toml"
    before = path.read_bytes()

    async def apply_configuration():
        async with engine.runtime_transition():
            entered.set()
            await release.wait()

    task = asyncio.create_task(apply_configuration())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ProfileRevisionConflict):
            application_config.save(
                {"NEUROENGINE.queue_size": 64}, expected_revision=application_config.read()[1])
        assert application_config._settings.get("NEUROENGINE.queue_size") == 32
        assert path.read_bytes() == before
    finally:
        release.set()
        await task
    application_config.save(
        {"NEUROENGINE.queue_size": 64}, expected_revision=application_config.read()[1])
    assert OmniBCISettings(path).get("NEUROENGINE.queue_size") == 64


@pytest.mark.asyncio
async def test_host_save_excludes_runtime_apply_until_persisted(application_config, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    engine = NeuroEngineService(engine_factory=MagicMock())
    application_config._change_guard = engine.configuration_change
    update = application_config._settings.update
    revision = application_config.read()[1]

    def persist(updates, *, expected_revision):
        entered.set()
        assert release.wait(3)
        return update(updates, expected_revision=expected_revision)

    monkeypatch.setattr(application_config._settings, "update", persist)
    thread = threading.Thread(target=lambda: application_config.save(
        {"NEUROENGINE.queue_size": 64}, expected_revision=revision))
    thread.start()
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        with pytest.raises(ProfileRevisionConflict):
            async with engine.runtime_transition():
                pytest.fail("Apply entered before the settings save completed")
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 3)
    assert not thread.is_alive()
    assert OmniBCISettings(tmp_path / "settings.toml").get("NEUROENGINE.queue_size") == 64


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
        profiles.accept(profiles.propose(bindings), expected_revision=accepted.revision if accepted else None)
        assert container.acquisition_service._device_metadata() == ("neuroscan_scan45_reader", None, None)
        with patch("omnibci.application.services.engine_service.Thread"):
            await container.engine_service.handle_start_engine(StartEngineCommand())
        snapshot = builder.call_args.kwargs["settings_snapshot"]
        assert snapshot["ACQUISITION.sample_rate"] == 500
        assert snapshot["ACQUISITION.DATA_PROVIDER_PORT"] == 4000
