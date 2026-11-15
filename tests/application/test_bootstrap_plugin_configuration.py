"""Composition selects accepted configuration instead of mutable saved profiles."""
from unittest.mock import Mock

import pytest

from omnibci import bootstrap
from omnibci.application.activation_settings import ENGINE_SETTING_DEFAULTS
from omnibci.bootstrap import _RuntimeSettings, build_container
from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.config_paths import config_root
from tests.plugin_configuration_helpers import accept_profiles


@pytest.fixture
def bootstrap_settings(tmp_path, monkeypatch):
    import omnibci.conf as conf

    path = tmp_path / "settings.toml"
    path.write_text('''[ACQUISITION]
signal_source = "file_reader"
data_file_path = "recordings/custom.cnt"
sample_rate = 256
channel_count = 5
file_reader_chunk_size = 17
trigger = ""
[ALGORITHM]
decoder = "THUBCI_SSVEP_classic40FBCCA"
epoch_detector = "DEFAULT_EPOCH_DETECTOR"
window_duration = 4.25
offset_time = 0.15
baseline_duration = 0.4
filters = []
[STIMULATION]
paradigm = "STIMULATION_THUBCI_SSVEP_classic40"
auto_spawn = false
[ACTUATION]
enabled = false
auto_spawn = false
effector = ""
[NEUROENGINE]
queue_size = 29
[custom]
retain = ["settings-only", "user-value"]
''', encoding="utf-8")
    settings = OmniBCISettings(path)
    monkeypatch.setattr(conf, "settings", settings)
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "plugins"))
    builder = Mock(side_effect=AssertionError("Bootstrap must not construct a worker graph"))
    monkeypatch.setattr(bootstrap, "NeuroEngineBuilder", builder)
    yield settings
    builder.assert_not_called()


def _toml_files(root):
    return {path: path.read_bytes() for path in root.rglob("*.toml")}


def _configure(container):
    return accept_profiles(container.plugin_profile_service, (
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": "recordings/custom.cnt", "sample_rate": 256,
            "channel_count": 5, "file_reader_chunk_size": 17,
        }),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40FBCCA", {
            "window_duration": 4.25, "offset_time": 0.15,
        }),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
    ))


@pytest.mark.asyncio
async def test_saved_edits_do_not_change_explicitly_selected_startup():
    container = await build_container()
    profiles = container.plugin_profile_service
    accepted = _configure(container)
    before = container.engine_service._module_snapshot_provider("acquisition")
    source = next(binding for binding in accepted.bindings() if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    parameters = {**profile.parameters, "sample_rate": 250}
    profiles.update(profile.profile_id, name="Saved edit", parameters=parameters,
                    expected_revision=profile.revision)

    assert container.engine_service._module_snapshot_provider("acquisition") == before
    assert before["CONFIG.module_revisions"]["acquisition"] == accepted.modules["acquisition"].revision
    assert [binding["slot"] for binding in before["PLUGINS.bindings"]] == ["acquisition.source"]
    assert container.engine_service._snapshot_provider()["PLUGINS.bindings"] == []


@pytest.mark.asyncio
async def test_fresh_installation_does_not_select_plugins(monkeypatch):
    import omnibci.conf as conf

    monkeypatch.setattr(conf, "settings", OmniBCISettings())
    container = await build_container()

    profiles = container.plugin_profile_service
    assert all(value is None for value in profiles.repository.get_activation_set().revisions().values())
    assert profiles.repository.list_profiles() == []
    assert container.acquisition_service._device_metadata() == (None, None, None)
    snapshot = container.engine_service._snapshot_provider()
    assert snapshot["PLUGINS.bindings"] == []
    assert profiles.repository.get_engine() is not None
    assert all(module is None for module in profiles.repository.get_activation_set().modules.values())


def test_status_reads_applied_values_while_engine_running():
    engine = Mock(is_engine_ready=True, applied_snapshot={"ACQUISITION.sample_rate": 250})
    profiles = Mock()
    profiles.repository.get_module.return_value = None
    profiles.repository.get_engine.return_value = None
    profiles.materialize.return_value = {"ACQUISITION.sample_rate": 1000}
    view = _RuntimeSettings(engine, profiles)
    assert view.get("ACQUISITION.sample_rate") == 250
    profiles.materialize.assert_not_called()
    engine.is_engine_ready = False
    assert view.get("ACQUISITION.sample_rate") == 1000


@pytest.mark.asyncio
async def test_stream_prepares_only_processing_and_keeps_receipt(monkeypatch):
    from omnibci.application.services.signal_setup_service import SignalSetupService
    calls = []
    plan = {"schema_version": 2}
    monkeypatch.setattr(SignalSetupService, "prepare", lambda self: calls.append("processing") or plan)
    container = await build_container()
    container.actuation_process_manager.start = Mock()

    assert container.acquisition_service._prepare_signal() == plan
    assert calls == ["processing"]
    container.actuation_process_manager.start.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_binding_uses_the_running_source_without_reading_saved_profiles():
    from omnibci.bootstrap import _acquisition_binding
    container = await build_container()
    profiles = container.plugin_profile_service
    accepted = _configure(container)
    binding = next(item for item in accepted.bindings() if item.slot == "acquisition.source")
    engine = Mock(applied_snapshot=profiles.materialize(accepted))
    assert _acquisition_binding(engine) == binding.to_dict()
    profile = profiles.repository.get_profile(binding.profile_id)
    profiles.update(profile.profile_id, name=profile.name,
                    parameters={**profile.parameters, "sample_rate": 250}, expected_revision=profile.revision)
    assert _acquisition_binding(engine) == binding.to_dict()
    engine.applied_snapshot = None
    assert _acquisition_binding(engine) is None


@pytest.mark.asyncio
async def test_bootstrap_ignores_retired_plugin_settings(bootstrap_settings):
    original_settings = bootstrap_settings._config_path.read_bytes()

    container = await build_container()

    assert all(value is None for value in container.plugin_profile_service.repository.get_activation_set().revisions().values())
    assert container.plugin_profile_service.repository.list_profiles() == []
    assert bootstrap_settings._config_path.read_bytes() == original_settings
    snapshot = container.engine_service._snapshot_provider()
    assert {key: snapshot[key] for key in ENGINE_SETTING_DEFAULTS} == ENGINE_SETTING_DEFAULTS
    assert container.plugin_profile_service.repository.get_engine().settings == ENGINE_SETTING_DEFAULTS


@pytest.mark.asyncio
async def test_server_restart_changes_command_timeout_without_blocking_engine_review(bootstrap_settings):
    first = await build_container()
    before = _configure(first)
    bootstrap_settings.set("NEUROENGINE.command_timeout_s", 15.0)
    bootstrap_settings.save()
    original_files = _toml_files(config_root())

    plan = first.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                            settings=before.engine.settings)
    assert plan.target_revisions["engine"] == before.engine.revision
    assert not plan.errors
    assert first.command_results._timeout_s == 10.0
    assert _toml_files(config_root()) == original_files

    restarted = await build_container()

    assert restarted.command_results._timeout_s == 15.0
    plan = restarted.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                                settings=before.engine.settings)
    assert plan.target_revisions["engine"] == before.engine.revision
    assert plan.changed_settings == ()
    assert not plan.errors
    assert _toml_files(config_root()) == original_files


@pytest.mark.asyncio
async def test_plugin_directory_edits_do_not_block_engine_review(bootstrap_settings, monkeypatch, tmp_path):
    container = await build_container()
    before = _configure(container)
    bootstrap_settings.set("PLUGINS.user_dir", str(tmp_path / "masked-by-environment"))
    bootstrap_settings.save()
    original_files = _toml_files(config_root())

    plan = container.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                                settings=before.engine.settings)

    assert plan.target_revisions["engine"] == before.engine.revision
    assert not plan.errors
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "changed-effective-path"))
    plan = container.runtime_configuration.plan("engine", [], expected_revision=before.engine.revision,
                                                settings=before.engine.settings)
    assert not plan.errors
    assert _toml_files(config_root()) == original_files


@pytest.mark.asyncio
async def test_existing_activation_keeps_its_revision_and_pinned_inputs(bootstrap_settings):
    first = await build_container()
    profiles = first.plugin_profile_service
    current = _configure(first)
    historical = current
    source = next(binding for binding in historical.bindings() if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    profiles.update(profile.profile_id, name="Saved source edit",
                    parameters={**profile.parameters, "sample_rate": 1024},
                    expected_revision=profile.revision)
    bootstrap_settings.set("ACQUISITION.sample_rate", 128)
    bootstrap_settings.set("ALGORITHM.decoder", "not-a-selected-plugin")
    bootstrap_settings.save()
    original_files = _toml_files(profiles.repository.root)
    original_settings = bootstrap_settings._config_path.read_bytes()

    restarted = await build_container()

    assert restarted.plugin_profile_service.repository.get_activation_set() == historical
    runtime = restarted.engine_service._module_snapshot_provider("acquisition")
    assert runtime["CONFIG.module_revisions"]["acquisition"] == historical.modules["acquisition"].revision
    assert runtime["ACQUISITION.sample_rate"] == 256
    assert runtime["ACQUISITION.channel_count"] == 5
    assert not {"ALGORITHM.window_duration", "ssvep.duration",
                "ACQUISITION.parallel_port_address"} & runtime.keys()
    processing_runtime = restarted.engine_service._module_snapshot_provider("processing")
    decoder = next(binding for binding in processing_runtime["PLUGINS.bindings"]
                   if binding["kind"] == "decoder")
    selected_decoder = next(binding for binding in historical.bindings() if binding.kind == "decoder")
    assert decoder["selection"] == selected_decoder.selection.to_dict()
    assert decoder["parameters"] == {"window_duration": 4.25, "offset_time": 0.15}
    assert _toml_files(profiles.repository.root) == original_files
    assert bootstrap_settings._config_path.read_bytes() == original_settings




@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", "invalid"),
    ("ALGORITHM.decoder", "missing_decoder"),
])
async def test_invalid_retired_settings_do_not_affect_management(bootstrap_settings, key, value):
    bootstrap_settings.set(key, value)
    bootstrap_settings.save()
    container = await build_container()

    assert all(value is None for value in container.plugin_profile_service.repository.get_activation_set().revisions().values())
    assert container.plugin_profile_service.repository.list_profiles() == []
    assert container.acquisition_service._device_metadata() == (None, None, None)
    assert container.config_service.list_entries()[0]
    assert container.engine_service._snapshot_provider()["PLUGINS.bindings"] == []


@pytest.mark.asyncio
async def test_broken_activation_keeps_management_available_without_replacement(bootstrap_settings):
    first = await build_container()
    repository = first.plugin_profile_service.repository
    path = repository.root / "activation" / "acquisition.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('format_version = "unterminated', encoding="utf-8")
    original_files = _toml_files(repository.root)

    container = await build_container()

    assert container.config_service.list_entries()[0]
    assert container.acquisition_service._device_metadata() == (None, None, None)
    foundation = container.engine_service._snapshot_provider()
    assert foundation["PLUGINS.bindings"] == []
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        container.engine_service._module_snapshot_provider("acquisition")
    assert path.read_bytes() == original_files[path]
    assert repository.get_engine() is not None


@pytest.mark.asyncio
async def test_saved_edit_during_bootstrap_does_not_change_loaded_startup_baseline(
    bootstrap_settings, monkeypatch,
):
    loaded = bootstrap_settings.saved_snapshot()
    bootstrap_settings.set("NEUROENGINE.command_timeout_s", 20.0)
    bootstrap_settings.save()
    container = await build_container(host_settings=loaded)
    service = container.plugin_profile_service

    assert container.command_results._timeout_s == 10.0
    candidate = service.propose_engine(ENGINE_SETTING_DEFAULTS)
    assert candidate.settings == ENGINE_SETTING_DEFAULTS
    assert "NEUROENGINE.command_timeout_s" not in candidate.settings
