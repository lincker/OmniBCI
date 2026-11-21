"""TOML persistence, optimistic edits and interprocess writer exclusion."""
import multiprocessing as mp
from dataclasses import replace
from uuid import uuid4

import pytest
import tomlkit

from omnibci.application.plugin_identity import PluginSelection
from omnibci.application.configuration.models import ModuleActivation, PluginProfile, ProfileBinding, EngineSettings
from omnibci.application.configuration.errors import PluginProfileNotFoundError, PluginProfileStorageError, PluginProfileValidationError, ProfileRevisionConflict
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


@pytest.fixture
def repository(tmp_path):
    return TomlConfigurationRepository(tmp_path / "config")


@pytest.fixture
def profile():
    return PluginProfile(str(uuid4()), "Lab A", "acquisition",
                         PluginSelection("acme.device", "reader", "1.0.0"),
                         {"host": "localhost", "channels": [1, 2]})


def _module(profile):
    return ModuleActivation("acquisition", {}, (ProfileBinding(
        "acquisition.source", profile.kind, profile.profile_id, profile.selection,
        profile.parameters, profile.configuration_version, profile.revision,
    ),))


def test_missing_reads_do_not_create_storage(repository):
    assert repository.list_profiles() == []
    assert repository.get_module("acquisition") is None
    assert repository.get_engine() is None
    assert all(item is None for item in repository.get_activation_set().revisions().values())
    with pytest.raises(PluginProfileNotFoundError):
        repository.get_profile(str(uuid4()))
    assert not repository.root.exists()


def test_profile_save_writes_integer_history_and_preserves_current(repository, profile):
    saved = repository.save_profile(profile)
    renamed = repository.save_profile(replace(saved, name="Lab B"), expected_revision=1)
    fresh = TomlConfigurationRepository(repository.root)
    assert renamed.revision == 2
    assert fresh.get_profile(profile.profile_id) == renamed
    assert fresh.list_profiles("acquisition") == [renamed]
    entries = fresh.list_profile_history(profile.profile_id)
    assert [item.revision for item in entries] == [2, 1]
    assert entries[1].profile == saved
    assert fresh.get_profile_revision(profile.profile_id, 1) == entries[1]
    assert fresh.get_profile_revision(profile.profile_id, 99) is None
    entries[0].profile.parameters["channels"].append(4)
    assert fresh.get_profile(profile.profile_id).parameters["channels"] == [1, 2]
    assert (repository.root / "plugin-profiles" / profile.profile_id / "current.toml").is_file()


def test_invalid_stored_selection_is_reported_without_replacing_the_profile(repository, profile):
    saved = repository.save_profile(profile)
    path = repository.root / "plugin-profiles" / saved.profile_id / "current.toml"
    document = tomlkit.parse(path.read_text())
    document["selection"]["version"] = "1.0.0."
    path.write_text(tomlkit.dumps(document))
    invalid = path.read_bytes()

    with pytest.raises(ValueError, match="exact package version"):
        repository.get_profile(saved.profile_id)
    with pytest.raises(ValueError, match="exact package version"):
        repository.list_profiles()
    assert path.read_bytes() == invalid


def test_engine_namespace_change_preserves_existing_settings_modules_and_history(repository, profile):
    saved = repository.save_profile(profile)
    modules = {"acquisition": repository.save_module(_module(saved))}
    for module in ("processing", "stimulation", "actuation"):
        modules[module] = repository.save_module(ModuleActivation(module, {}, ()))
    for module in modules.values():
        repository.record_module_history(module, str(uuid4()))
    path = repository.root / "settings.toml"
    path.write_text('[engine]\n"NEUROENGINE.queue_size" = 100\ndebug = false\n', encoding="utf-8")
    before = {item: item.read_bytes() for item in repository.root.rglob("*.toml")}

    accepted = repository.get_activation_set()

    assert accepted.engine.settings == {"NEUROENGINE.queue_size": 100, "debug": False}
    assert accepted.engine.revision != "388c8997fdb5461903849aa0f8dd1072e1a5db9d24490096a25c77cfbfd1fe27"
    assert dict(accepted.modules) == modules
    assert repository.get_profile_revision(saved.profile_id, 1).profile == saved
    for name, module in modules.items():
        assert repository.list_module_history(name)[0].activation == module
    assert {item: item.read_bytes() for item in repository.root.rglob("*.toml")} == before


def test_stale_profile_writes_and_owning_capability_change_are_rejected(repository, profile):
    saved = repository.save_profile(profile)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_profile(profile)
    updated = repository.save_profile(replace(saved, name="Updated"), expected_revision=1)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_profile(saved, expected_revision=1)
    with pytest.raises(ProfileRevisionConflict):
        repository.delete_profile(profile.profile_id, expected_revision=1)
    with pytest.raises(PluginProfileValidationError, match="owning"):
        repository.save_profile(replace(updated, selection=replace(updated.selection, contribution_id="other")),
                                expected_revision=2)
    assert repository.get_profile(profile.profile_id) == updated


def test_delete_is_blocked_by_any_current_module_and_removes_profile_history(repository, profile):
    saved = repository.save_profile(profile)
    activation = repository.save_module(_module(saved))
    saved = repository.save_profile(replace(saved, name="Updated"), expected_revision=1)
    with pytest.raises(ProfileRevisionConflict, match="selected"):
        repository.delete_profile(saved.profile_id, expected_revision=2)
    assert repository.get_module("acquisition") == activation
    repository.save_module(ModuleActivation("acquisition", activation.settings, ()), expected_revision=activation.revision)
    repository.delete_profile(saved.profile_id, expected_revision=2)
    assert not (repository.root / "plugin-profiles" / saved.profile_id).exists()
    with pytest.raises(PluginProfileNotFoundError):
        repository.list_profile_history(saved.profile_id)


def test_modules_have_independent_cas_and_no_implicit_history(repository):
    original = repository.save_module(ModuleActivation("processing", {"ALGORITHM.baseline_duration": 1.0}, ()))
    changed = ModuleActivation("processing", {"ALGORITHM.baseline_duration": 2.0}, ())
    stimulation = repository.save_module(ModuleActivation("stimulation", {"STIMULATION.auto_spawn": False}, ()))
    with pytest.raises(ProfileRevisionConflict):
        repository.save_module(changed)
    repository.save_module(changed, expected_revision=original.revision)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_module(original, expected_revision=original.revision)
    assert repository.get_module("stimulation") == stimulation
    assert repository.list_module_history("processing") == []
    assert repository.get_activation_set().modules["processing"] == changed


def test_module_history_records_a_b_a_as_three_immutable_entries(repository):
    first = ModuleActivation("processing", {"ALGORITHM.baseline_duration": 1.0}, ())
    second = ModuleActivation("processing", {"ALGORITHM.baseline_duration": 2.0}, ())
    entries = [repository.record_module_history(item, str(uuid4())) for item in (first, second, first)]
    assert [item.entry_id for item in repository.list_module_history("processing")] == [
        item.entry_id for item in reversed(entries)]
    assert entries[0].revision == entries[2].revision
    assert entries[0].entry_id != entries[2].entry_id
    assert repository.get_module("processing") is None
    retried = repository.record_module_history(first, entries[0].entry_id)
    assert retried == entries[0]
    assert len(repository.list_module_history("processing")) == 3
    with pytest.raises(ProfileRevisionConflict, match="different"):
        repository.record_module_history(second, entries[0].entry_id)
    restored = repository.record_module_history(first, str(uuid4()), restored_from=entries[0].entry_id)
    assert repository.get_module_history_entry("processing", restored.entry_id) == restored
    assert restored.restored_from == entries[0].entry_id


def test_rollback_current_writes_do_not_add_history(repository):
    first = repository.save_module(ModuleActivation("processing", {}, ()))
    entry = repository.record_module_history(first, str(uuid4()))
    failed_candidate = repository.save_module(ModuleActivation("processing", {"ALGORITHM.baseline_duration": 1.0}, ()),
                                               expected_revision=first.revision)
    repository.save_module(first, expected_revision=failed_candidate.revision)
    assert repository.list_module_history("processing") == [entry]


def test_engine_cas_and_compensation_restore_absence_without_history(repository):
    original = repository.save_engine(EngineSettings({"debug": False}))
    with pytest.raises(ProfileRevisionConflict):
        repository.save_engine(EngineSettings({"debug": True}))
    with pytest.raises(ProfileRevisionConflict):
        repository.remove_engine(expected_revision="0" * 64)
    repository.remove_engine(expected_revision=original.revision)
    assert repository.get_engine() is None
    original_module = repository.save_module(ModuleActivation("actuation", {}, ()))
    repository.remove_module("actuation", expected_revision=original_module.revision)
    assert repository.get_module("actuation") is None
    assert not (repository.root / "activation" / "history" / "engine").exists()


def test_different_modules_do_not_share_thread_or_file_lock(repository):
    repository.save_module(ModuleActivation("processing", {}, ()))
    other = TomlConfigurationRepository(repository.root, lock_timeout_s=0.05)
    with repository._access("processing", write=True):
        stimulation = other.save_module(ModuleActivation("stimulation", {}, ()))
        actuation = repository.save_module(ModuleActivation("actuation", {}, ()))
        with pytest.raises(PluginProfileStorageError, match="lock"):
            other.save_module(ModuleActivation("processing", {}, ()))
    assert repository.get_module("stimulation") == stimulation
    assert repository.get_module("actuation") == actuation


def test_failed_current_replace_cleans_uncommitted_profile_history(repository, profile, monkeypatch):
    saved = repository.save_profile(profile)
    write = repository._write

    def fail_current(path, data):
        if path.name == "current.toml":
            raise OSError("disk full")
        write(path, data)

    monkeypatch.setattr(repository, "_write", fail_current)
    with pytest.raises(PluginProfileStorageError, match="disk full"):
        repository.save_profile(replace(saved, name="Failed"), expected_revision=1)
    assert repository.get_profile(profile.profile_id) == saved
    assert [entry.revision for entry in repository.list_profile_history(profile.profile_id)] == [1]
    monkeypatch.setattr(repository, "_write", write)
    updated = repository.save_profile(replace(saved, name="Retry with different values"), expected_revision=1)
    assert repository.get_profile_revision(profile.profile_id, 2).profile == updated


def test_orphan_history_after_crash_is_hidden_and_replaced_on_retry(repository, profile):
    saved = repository.save_profile(profile)
    history = repository.root / "plugin-profiles" / profile.profile_id / "history"
    orphan = repository.get_profile_revision(profile.profile_id, 1).to_dict()
    orphan["revision"] = orphan["profile"]["revision"] = 2
    orphan["profile"]["name"] = "Uncommitted"
    (history / "2.toml").write_text(tomlkit.dumps(orphan), encoding="utf-8")
    assert [entry.revision for entry in repository.list_profile_history(profile.profile_id)] == [1]
    updated = repository.save_profile(replace(saved, name="Committed"), expected_revision=1)
    assert repository.get_profile_revision(profile.profile_id, 2).profile == updated


def test_failed_module_replace_preserves_current_and_history(repository, monkeypatch):
    saved = repository.save_module(ModuleActivation("processing", {}, ()))
    entry = repository.record_module_history(saved, str(uuid4()))
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("omnibci.infrastructure.repositories.configuration_storage.os.replace", fail)
    with pytest.raises(PluginProfileStorageError, match="disk full"):
        repository.save_module(ModuleActivation("processing", {"ALGORITHM.baseline_duration": 1.0}, ()),
                               expected_revision=saved.revision)
    assert repository.get_module("processing") == saved
    assert repository.list_module_history("processing") == [entry]
    assert not list(repository.root.rglob(".*.toml.*"))


def test_old_storage_paths_are_ignored(repository, profile):
    repository.root.mkdir()
    (repository.root / "activation.toml").write_text("invalid old activation", encoding="utf-8")
    directory = repository.root / "plugin-profiles"
    directory.mkdir()
    (directory / f"{profile.profile_id}.toml").write_text("invalid old profile", encoding="utf-8")
    assert repository.list_profiles() == []
    assert all(value is None for value in repository.get_activation_set().revisions().values())


@pytest.mark.parametrize("bad_id", ["../outside", "", "not-a-uuid"])
def test_paths_require_canonical_ids(repository, bad_id):
    with pytest.raises(PluginProfileValidationError):
        repository.get_profile(bad_id)
    with pytest.raises(PluginProfileValidationError):
        repository.get_module_history_entry("processing", bad_id)
    assert not repository.root.exists()


@pytest.mark.parametrize("directory", ["plugin-profiles", "activation", "locks"])
def test_symlink_cannot_redirect_writes(repository, profile, tmp_path, directory):
    outside = tmp_path / "outside"
    outside.mkdir()
    repository.root.mkdir()
    try:
        (repository.root / directory).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    with pytest.raises(PluginProfileValidationError):
        if directory == "activation":
            repository.save_module(ModuleActivation("processing", {}, ()))
        else:
            repository.save_profile(profile)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_lock_timeout_must_remain_bounded(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite"):
        TomlConfigurationRepository(tmp_path, lock_timeout_s=timeout)


def _competing_update(root, profile_id, barrier, output, name):
    repository = TomlConfigurationRepository(root)
    profile = repository.get_profile(profile_id)
    barrier.wait(timeout=15)
    try:
        repository.save_profile(replace(profile, name=name), expected_revision=profile.revision)
    except ProfileRevisionConflict:
        output.put("conflict")
    else:
        output.put("saved")


@pytest.mark.integration
def test_competing_processes_cannot_both_save_same_revision(repository, profile):
    repository.save_profile(profile)
    context = mp.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_competing_update,
                                args=(repository.root, profile.profile_id, barrier, output, name))
                 for name in ("A", "B")]
    for process in processes:
        process.start()
    try:
        assert sorted(output.get(timeout=20) for _ in processes) == ["conflict", "saved"]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert repository.get_profile(profile.profile_id).revision == 2


def test_listing_acquisition_does_not_validate_dormant_decoder_input(repository, profile):
    saved = repository.save_profile(profile)
    decoder_id = str(uuid4())
    path = repository.root / "plugin-profiles" / decoder_id / "current.toml"
    path.parent.mkdir(parents=True)
    data = {**saved.to_dict(), "profile_id": decoder_id, "kind": "decoder",
            "processing_input": {"schema_version": 2, "outputs": [{"name": "Missing source"}]}}
    path.write_text(tomlkit.dumps(data), encoding="utf-8")
    assert repository.list_profiles("acquisition") == [saved]
    with pytest.raises((PluginProfileValidationError, ValueError)):
        repository.list_profiles("decoder")
