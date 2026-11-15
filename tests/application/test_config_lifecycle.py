"""Server saves and Engine application share storage without lifecycle coupling."""
from omnibci.application.activation_settings import section_settings
import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

from omnibci.application.commands.engine_commands import StartEngineCommand
from omnibci.application.plugin_profiles import EngineSettings
from omnibci.application.services.config_service import ConfigService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def application_config(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\ncommand_timeout_s=10.0\n[engine]\n"NEUROENGINE.queue_size"=32\n')
    return ConfigService(OmniBCISettings(user_file=path))


def test_server_save_does_not_apply_to_running_engine(application_config):
    builder = MagicMock()
    engine = NeuroEngineService(engine_factory=builder)
    engine._running = True
    engine._applied_snapshot = {"NEUROENGINE.queue_size": 32}
    saved, restart, _ = application_config.save(
        {"NEUROENGINE.command_timeout_s": 20}, expected_revision=application_config.read()[1])
    assert saved == ["NEUROENGINE.command_timeout_s"] and restart
    assert engine.applied_snapshot == {"NEUROENGINE.queue_size": 32}
    assert next(item for item in application_config.list_entries()
                if item["key"] == "NEUROENGINE.command_timeout_s")["active_value"] == 10
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_server_save_does_not_wait_for_engine_transition(application_config, tmp_path):
    engine = NeuroEngineService(engine_factory=MagicMock())
    repository = TomlPluginProfileRepository(tmp_path)
    before = repository.get_engine()
    async with engine.runtime_transition():
        application_config.save({"LOGGING.level": "DEBUG"}, expected_revision=application_config.read()[1])
    assert repository.get_engine() == before


def test_server_save_preserves_engine_change_made_during_request(application_config, monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    repository = TomlPluginProfileRepository(tmp_path)
    before = repository.get_engine()
    update = application_config._settings.update
    revision = application_config.read()[1]
    errors = []
    def persist(updates, *, expected_revision):
        entered.set()
        assert release.wait(3)
        return update(updates, expected_revision=expected_revision)
    monkeypatch.setattr(application_config._settings, "update", persist)
    def save():
        try:
            application_config.save({"LOGGING.level": "DEBUG"}, expected_revision=revision)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=save)
    thread.start()
    try:
        assert entered.wait(3)
        repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 64}), expected_revision=before.revision)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and not errors
    assert repository.get_engine().settings == {"NEUROENGINE.queue_size": 64}
    assert OmniBCISettings(user_file=tmp_path / "settings.toml").saved_snapshot()["LOGGING.level"] == "DEBUG"


@pytest.mark.asyncio
async def test_bootstrap_shares_pinned_plugin_selection_between_status_and_engine(application_config, monkeypatch, tmp_path):
    import omnibci.conf as conf
    from omnibci import bootstrap
    from tests.plugin_configuration_helpers import builtin_selection

    monkeypatch.setattr(conf, "settings", application_config._settings)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    with patch.object(bootstrap, "NeuroEngineBuilder") as builder:
        container = await bootstrap.build_container()
        builder.assert_not_called()
        profiles = container.plugin_profile_service
        selected = profiles.create("acquisition", builtin_selection(
            "acquisition", "neuroscan_scan45_reader"), "Lab", {
                "DATA_PROVIDER_IP": "192.168.0.3", "DATA_PROVIDER_PORT": 4000,
                "sample_rate": 500, "channel_count": 10,
            })
        bindings = [{"slot": "acquisition.source", "profile_id": selected.profile_id}]
        for slot, kind, alias in (
            ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA"),
            ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR"),
            ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40"),
        ):
            selected = profiles.create(kind, builtin_selection(kind, alias), alias, {})
            bindings.append({"slot": slot, "profile_id": selected.profile_id})
        profiles.repository.save_engine(profiles.propose_engine({"NEUROENGINE.queue_size": 32}), expected_revision=None)
        for module in ("acquisition", "processing", "stimulation"):
            profiles.repository.save_module(profiles.propose(module, [
                binding for binding in bindings if binding["slot"].startswith(f"{module}.")
            ], settings=section_settings(profiles.settings_source(), module)), expected_revision=None)
        assert container.acquisition_service._device_metadata() == ("neuroscan_scan45_reader", None, None)
        with patch("omnibci.application.services.engine_service.Thread"):
            await container.engine_service.handle_start_engine(StartEngineCommand())
        snapshot = builder.call_args.kwargs["settings_snapshot"]
        assert snapshot["PLUGINS.bindings"] == []
        source_snapshot = container.engine_service._module_snapshot_provider("acquisition")
        assert source_snapshot["ACQUISITION.sample_rate"] == 500
        assert source_snapshot["ACQUISITION.DATA_PROVIDER_PORT"] == 4000
