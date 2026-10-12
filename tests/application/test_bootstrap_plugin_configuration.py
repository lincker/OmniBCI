"""Composition selects accepted configuration instead of mutable saved profiles."""
from unittest.mock import Mock
from dataclasses import replace

import pytest

from omnibci import bootstrap
from omnibci.application.plugin_profiles import ActivationSnapshot
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.domain.montage import ProcessingInput
from omnibci.bootstrap import _RuntimeSettings, build_container
from omnibci.conf import OmniBCISettings
from omnibci.infrastructure.config_paths import config_root
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def bootstrap_settings(tmp_path, monkeypatch):
    import omnibci.conf as conf

    path = tmp_path / "settings.toml"
    path.write_text('''[ACQUISITION]
signal_source = "file_reader"
data_file_path = "recordings/custom.cnt"
sample_rate = 256
channel_number = 5
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


@pytest.mark.asyncio
async def test_composition_initializes_once_and_saved_edits_do_not_change_startup():
    container = await build_container()
    profiles = container.plugin_profile_service
    accepted = profiles.repository.get_activation()
    assert accepted is not None
    before = container.engine_service._snapshot_provider()
    source = next(binding for binding in accepted.bindings if binding.kind == "acquisition")
    profile = profiles.repository.get_profile(source.profile_id)
    parameters = {**profile.parameters, "sample_rate": 250}
    profiles.update(profile.profile_id, name="Saved edit", parameters=parameters,
                    expected_revision=profile.revision)

    assert container.engine_service._snapshot_provider() == before
    assert before["CONFIG.revision"] == accepted.revision
    assert len(before["PLUGINS.bindings"]) >= 4


def test_status_reads_applied_values_while_engine_running():
    engine = Mock(is_engine_ready=True, applied_snapshot={"ACQUISITION.sample_rate": 250})
    profiles = Mock()
    profiles.materialize.return_value = {"ACQUISITION.sample_rate": 1000}
    view = _RuntimeSettings(engine, profiles)
    assert view.get("ACQUISITION.sample_rate") == 250
    profiles.materialize.assert_not_called()
    engine.is_engine_ready = False
    assert view.get("ACQUISITION.sample_rate") == 1000


@pytest.mark.asyncio
async def test_montage_binding_uses_applied_source_and_guards_explicit_profile_reads():
    from omnibci.application.plugin_profiles import ProfileRevisionConflict
    from omnibci.bootstrap import _acquisition_montage_binding
    container = await build_container()
    profiles = container.plugin_profile_service
    accepted = profiles.repository.get_activation()
    binding = next(item for item in accepted.bindings if item.slot == "acquisition.source")
    engine = Mock(applied_snapshot=profiles.materialize(accepted))
    assert _acquisition_montage_binding(engine, profiles, binding.profile_id) == binding
    with pytest.raises(ProfileRevisionConflict, match="applied acquisition profile"):
        _acquisition_montage_binding(engine, profiles, "another-profile")
    profile = profiles.repository.get_profile(binding.profile_id)
    profiles.update(profile.profile_id, name=profile.name,
                    parameters={**profile.parameters, "sample_rate": 250}, expected_revision=profile.revision)
    with pytest.raises(ProfileRevisionConflict, match="differs from the running source"):
        _acquisition_montage_binding(engine, profiles, binding.profile_id)
    # An automatic observation still belongs to the running profile's pinned copy.
    assert _acquisition_montage_binding(engine, profiles) == binding


@pytest.mark.asyncio
async def test_settings_only_initialization_preserves_nondefault_values(bootstrap_settings):
    original_settings = bootstrap_settings._config_path.read_bytes()

    container = await build_container()

    accepted = container.plugin_profile_service.repository.get_activation()
    bindings = {binding.slot: binding for binding in accepted.bindings}
    assert bindings["acquisition.source"].parameters == {
        "data_file_path": "recordings/custom.cnt", "sample_rate": 256,
        "channel_number": 5, "file_reader_chunk_size": 17,
    }
    assert bindings["processing.decoder"].parameters == {"window_duration": 4.25, "offset_time": 0.15}
    assert bindings["processing.decoder"].processing_input == ProcessingInput()
    runtime = container.engine_service._snapshot_provider()
    assert "SIGNAL.setup" not in runtime
    assert next(binding for binding in runtime["PLUGINS.bindings"]
                if binding["slot"] == "processing.decoder")["processing_input"] == ProcessingInput().to_dict()
    assert "stimulation.trigger" not in bindings
    assert "actuation.effector" not in bindings
    assert accepted.settings == {
        "ALGORITHM.baseline_duration": 0.4,
        "STIMULATION.auto_spawn": False,
        "ACTUATION.enabled": False,
        "ACTUATION.auto_spawn": False,
        "NEUROENGINE.queue_size": 29,
        "custom.retain": ["settings-only", "user-value"],
    }
    assert bootstrap_settings._config_path.read_bytes() == original_settings


@pytest.mark.asyncio
async def test_host_review_compares_loaded_backend_values_after_restart(bootstrap_settings):
    first = await build_container()
    before = first.plugin_profile_service.repository.get_activation()
    references = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in before.bindings]
    bootstrap_settings.set("NEUROENGINE.command_timeout_s", 15.0)
    bootstrap_settings.save()
    original_files = _toml_files(config_root())

    with pytest.raises(PluginSchemaError, match="Backend restart required.*NEUROENGINE.command_timeout_s"):
        first.runtime_configuration.plan(references, expected_revision=before.revision,
                                         include_host_settings=True, refresh_slots=[])
    assert first.command_results._timeout_s == 10.0
    assert _toml_files(config_root()) == original_files

    restarted = await build_container()

    assert restarted.command_results._timeout_s == 15.0
    plan = restarted.runtime_configuration.plan(references, expected_revision=before.revision,
                                                include_host_settings=True, refresh_slots=[])
    assert plan.target_revision == before.revision
    assert plan.changed_settings == ()
    assert not plan.errors
    assert _toml_files(config_root()) == original_files


@pytest.mark.asyncio
async def test_plugin_directory_restart_check_uses_effective_environment_path(bootstrap_settings, monkeypatch, tmp_path):
    container = await build_container()
    before = container.plugin_profile_service.repository.get_activation()
    references = [{"slot": binding.slot, "profile_id": binding.profile_id} for binding in before.bindings]
    original_files = _toml_files(config_root())
    bootstrap_settings.set("PLUGINS.user_dir", str(tmp_path / "masked-by-environment"))
    bootstrap_settings.save()

    plan = container.runtime_configuration.plan(references, expected_revision=before.revision,
                                                include_host_settings=True, refresh_slots=[])

    assert plan.target_revision == before.revision
    assert not plan.errors
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "changed-effective-path"))
    with pytest.raises(PluginSchemaError, match="Backend restart required.*PLUGINS.user_dir"):
        container.runtime_configuration.plan(references, expected_revision=before.revision,
                                             include_host_settings=True, refresh_slots=[])
    assert _toml_files(config_root()) == original_files


@pytest.mark.asyncio
async def test_existing_activation_keeps_its_revision_and_pinned_inputs(bootstrap_settings):
    first = await build_container()
    profiles = first.plugin_profile_service
    current = profiles.repository.get_activation()
    historical = ActivationSnapshot(
        {**bootstrap_settings.snapshot(), "ACQUISITION.sample_rate": 111,
         "ALGORITHM.window_duration": 9.0, "ssvep.duration": 8.0,
         "ACQUISITION.parallel_port_address": "0x378"},
        current.bindings,
    )
    profiles.repository.save_activation(historical, expected_revision=current.revision)
    source = next(binding for binding in historical.bindings if binding.kind == "acquisition")
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

    assert restarted.plugin_profile_service.repository.get_activation() == historical
    runtime = restarted.engine_service._snapshot_provider()
    assert runtime["CONFIG.revision"] == historical.revision
    assert runtime["ACQUISITION.sample_rate"] == 256
    assert runtime["ACQUISITION.channel_number"] == 5
    assert runtime["ALGORITHM.decoder"] == "THUBCI_SSVEP_classic40FBCCA"
    assert not {"ALGORITHM.window_duration", "ssvep.duration",
                "ACQUISITION.parallel_port_address"} & runtime.keys()
    decoder = next(binding for binding in runtime["PLUGINS.bindings"]
                   if binding["kind"] == "decoder")
    assert decoder["parameters"] == {"window_duration": 4.25, "offset_time": 0.15}
    assert _toml_files(profiles.repository.root) == original_files
    assert bootstrap_settings._config_path.read_bytes() == original_settings


@pytest.mark.asyncio
async def test_bootstrap_keeps_legacy_decoder_input_absence_and_global_setup(bootstrap_settings):
    first = await build_container()
    profiles = first.plugin_profile_service
    current = profiles.repository.get_activation()
    setup = {"source_id": "file_reader", "outputs": ["O1"]}
    legacy = ActivationSnapshot({**current.settings, "SIGNAL.setup": setup}, tuple(
        replace(binding, processing_input=None) if binding.kind == "decoder" else binding
        for binding in current.bindings))
    profiles.repository.save_activation(legacy, expected_revision=current.revision)
    files = _toml_files(profiles.repository.root)

    restarted = await build_container()

    assert restarted.plugin_profile_service.repository.get_activation() == legacy
    assert restarted.engine_service._snapshot_provider()["SIGNAL.setup"] == setup
    assert _toml_files(profiles.repository.root) == files


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", "invalid"),
    ("ALGORITHM.decoder", "missing_decoder"),
])
async def test_failed_initialization_keeps_management_available(bootstrap_settings, key, value):
    bootstrap_settings.set(key, value)
    bootstrap_settings.save()
    container = await build_container()

    assert container.plugin_profile_service.repository.get_activation() is None
    assert container.plugin_profile_service.repository.list_profiles() == []
    assert container.acquisition_service._device_metadata() == (None, None, None)
    assert container.config_service.list_entries()[0]
    with pytest.raises(RuntimeError, match="not selected"):
        container.engine_service._snapshot_provider()


@pytest.mark.asyncio
async def test_broken_activation_keeps_management_available_without_replacement(bootstrap_settings):
    first = await build_container()
    repository = first.plugin_profile_service.repository
    path = repository.root / "activation.toml"
    path.write_text('format_version = "unterminated', encoding="utf-8")
    original_files = _toml_files(repository.root)

    container = await build_container()

    assert container.config_service.list_entries()[0]
    assert container.acquisition_service._device_metadata() == (None, None, None)
    with pytest.raises(RuntimeError, match="configuration is invalid"):
        container.engine_service._snapshot_provider()
    assert _toml_files(repository.root) == original_files


@pytest.mark.asyncio
async def test_saved_edit_during_bootstrap_does_not_change_loaded_startup_baseline(
    bootstrap_settings, monkeypatch,
):
    build_repository = bootstrap._build_session_repository

    def edit_saved_settings(kind):
        bootstrap_settings.set("NEUROENGINE.command_timeout_s", 20.0)
        bootstrap_settings.save()
        return build_repository(kind)

    monkeypatch.setattr(bootstrap, "_build_session_repository", edit_saved_settings)
    container = await build_container()
    service = container.plugin_profile_service
    accepted = service.repository.get_activation()
    bindings = [{"slot": binding.slot, "profile_id": binding.profile_id}
                for binding in accepted.bindings]

    assert container.command_results._timeout_s == 10.0
    with pytest.raises(PluginSchemaError, match="Backend restart required.*NEUROENGINE.command_timeout_s"):
        service.propose(bindings, include_host_settings=True, refresh_slots=[])


def test_effective_plugin_directory_uses_supplied_startup_snapshot(
    bootstrap_settings, monkeypatch, tmp_path,
):
    monkeypatch.delenv("OMNIBCI_PLUGIN_DIR")
    loaded_directory = tmp_path / "loaded-plugins"
    bootstrap_settings.set("PLUGINS.user_dir", str(loaded_directory))
    bootstrap_settings.save()
    loaded = bootstrap_settings.saved_snapshot()
    bootstrap_settings.set("PLUGINS.user_dir", str(tmp_path / "new-plugins"))
    bootstrap_settings.save()

    resolved = bootstrap._effective_backend_startup_settings(loaded)

    assert resolved["PLUGINS.user_dir"] == str(loaded_directory.resolve())
