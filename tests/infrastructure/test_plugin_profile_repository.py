"""TOML persistence, optimistic edits and interprocess writer exclusion."""
import multiprocessing as mp
from dataclasses import replace
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginProfileNotFoundError,
    PluginProfileStorageError, PluginProfileValidationError, PluginSelection,
    ProfileBinding, ProfileRevisionConflict,
)
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


@pytest.fixture
def repository(tmp_path):
    return TomlPluginProfileRepository(tmp_path / "config")


@pytest.fixture
def profile():
    return PluginProfile(str(uuid4()), "Lab A", "acquisition",
                         PluginSelection("acme.device", "reader", "1.0.0"),
                         {"host": "localhost", "channels": [1, 2]})


def _snapshot(profile):
    return ActivationSnapshot({"SYSTEM.mode": "test"}, (ProfileBinding(
        "acquisition.source", profile.kind, profile.profile_id, profile.selection,
        profile.parameters, profile.configuration_version, profile.revision,
    ),))


def test_missing_reads_do_not_create_storage(repository):
    assert repository.list_profiles() == []
    assert repository.get_activation() is None
    with pytest.raises(PluginProfileNotFoundError):
        repository.get_profile(str(uuid4()))
    assert not repository.root.exists()


def test_round_trip_and_optimistic_rename_preserve_identity(repository, profile):
    saved = repository.save_profile(profile)
    assert saved.revision == 1
    renamed = repository.save_profile(replace(saved, name="Lab B"), expected_revision=1)
    fresh = TomlPluginProfileRepository(repository.root)

    assert renamed.revision == 2
    assert fresh.get_profile(profile.profile_id) == renamed
    assert fresh.list_profiles("acquisition") == [renamed]
    assert fresh.list_profiles("decoder") == []
    assert list((repository.root / "plugin-profiles").glob("*.toml")) == [
        repository.root / "plugin-profiles" / f"{profile.profile_id}.toml",
    ]


def test_returned_profile_parameters_do_not_mutate_storage(repository, profile):
    saved = repository.save_profile(profile)
    saved.parameters["channels"].append(3)
    retrieved = repository.get_profile(profile.profile_id)
    retrieved.parameters["channels"].append(4)
    assert repository.get_profile(profile.profile_id).parameters["channels"] == [1, 2]


def test_stale_create_update_and_delete_do_not_overwrite(repository, profile):
    saved = repository.save_profile(profile)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_profile(profile)
    updated = repository.save_profile(replace(saved, name="Updated"), expected_revision=1)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_profile(saved, expected_revision=1)
    with pytest.raises(ProfileRevisionConflict):
        repository.delete_profile(profile.profile_id, expected_revision=1)
    assert repository.get_profile(profile.profile_id) == updated
    repository.delete_profile(profile.profile_id, expected_revision=2)
    with pytest.raises(PluginProfileNotFoundError):
        repository.get_profile(profile.profile_id)


def test_profile_saves_and_deletion_do_not_change_accepted_snapshot(repository, profile):
    saved = repository.save_profile(profile)
    snapshot = repository.save_activation(_snapshot(saved))
    saved = repository.save_profile(replace(saved, parameters={"host": "new", "channels": [3]}),
                                    expected_revision=1)
    repository.delete_profile(saved.profile_id, expected_revision=2)
    fresh = TomlPluginProfileRepository(repository.root)

    assert fresh.get_activation() == snapshot
    assert fresh.get_activation().bindings[0].parameters == profile.parameters
    assert fresh.list_profiles() == []


def test_activation_compare_and_swap_and_legacy_null_fields(repository):
    selection = PluginSelection("local.legacy", "default", None)
    binding = ProfileBinding("processing.decoder", "decoder", None, selection, {})
    original = repository.save_activation(ActivationSnapshot({}, (binding,)))
    changed = ActivationSnapshot({"mode": "new"}, original.bindings)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_activation(changed)
    repository.save_activation(changed, expected_revision=original.revision)
    with pytest.raises(ProfileRevisionConflict):
        repository.save_activation(original, expected_revision=original.revision)
    assert TomlPluginProfileRepository(repository.root).get_activation() == changed


@pytest.mark.parametrize("target", ["profile", "activation"])
def test_failed_replace_preserves_old_content(repository, profile, monkeypatch, target):
    saved = repository.save_profile(profile)
    snapshot = repository.save_activation(_snapshot(saved))
    paths = sorted(repository.root.rglob("*.toml"))
    before = {path: path.read_bytes() for path in paths}

    def fail_replace(*args):
        raise OSError("disk full")

    monkeypatch.setattr("omnibci.infrastructure.repositories.plugin_profile_repository.os.replace", fail_replace)
    with pytest.raises(PluginProfileStorageError, match="disk full"):
        if target == "profile":
            repository.save_profile(replace(saved, name="Changed"), expected_revision=1)
        else:
            repository.save_activation(ActivationSnapshot({"mode": "new"}, snapshot.bindings),
                                       expected_revision=snapshot.revision)
    assert {path: path.read_bytes() for path in paths} == before
    assert not list(repository.root.rglob(".*.toml.*"))


@pytest.mark.parametrize("bad_id", ["../outside", "", "not-a-uuid"])
def test_profile_paths_require_canonical_uuid(repository, bad_id):
    with pytest.raises(PluginProfileValidationError):
        repository.get_profile(bad_id)
    assert not repository.root.exists()


@pytest.mark.parametrize("replacement", ["malformed", "identity", "revision", "format"])
def test_malformed_storage_cannot_be_overwritten(repository, profile, replacement):
    repository.save_profile(profile)
    path = repository.root / "plugin-profiles" / f"{profile.profile_id}.toml"
    content = path.read_text()
    if replacement == "malformed":
        content = 'name = "unterminated'
    elif replacement == "identity":
        content = content.replace(profile.profile_id, str(uuid4()))
    elif replacement == "revision":
        content = content.replace("revision = 1", "revision = 0")
    else:
        content = content.replace("format_version = 2", "format_version = 99")
    path.write_text(content)
    with pytest.raises(PluginProfileValidationError):
        repository.save_profile(profile, expected_revision=1)
    assert path.read_text() == content


def test_symlink_cannot_redirect_profile_write(repository, profile, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    repository.root.mkdir()
    try:
        (repository.root / "plugin-profiles").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    with pytest.raises(PluginProfileValidationError):
        repository.save_profile(profile)
    assert list(outside.iterdir()) == []


def test_distinct_repository_instances_respect_writer_lock(repository, profile):
    saved = repository.save_profile(profile)
    second = TomlPluginProfileRepository(repository.root, lock_timeout_s=0.05)
    with repository._access(write=True):
        with pytest.raises(PluginProfileStorageError, match="lock"):
            second.save_profile(replace(saved, name="Blocked"), expected_revision=1)
    assert second.get_profile(saved.profile_id) == saved
    assert second.save_profile(replace(saved, name="Allowed"), expected_revision=1).revision == 2


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_lock_timeout_must_remain_bounded(tmp_path, timeout):
    with pytest.raises(ValueError, match="finite"):
        TomlPluginProfileRepository(tmp_path, lock_timeout_s=timeout)


def _competing_update(root, profile_id, barrier, output, name):
    repository = TomlPluginProfileRepository(root)
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
    spawn = mp.get_context("spawn")
    barrier, output = spawn.Barrier(2), spawn.Queue()
    processes = [spawn.Process(target=_competing_update,
                               args=(repository.root, profile.profile_id, barrier, output, name))
                 for name in ("Writer A", "Writer B")]
    try:
        for process in processes:
            process.start()
        assert sorted(output.get(timeout=20) for _ in processes) == ["conflict", "saved"]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert repository.get_profile(profile.profile_id).revision == 2
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.pid is not None:
                process.close()
        output.close()
        output.join_thread()
