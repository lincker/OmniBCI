"""Engine acceptance and backend writes share one user file without lost updates."""
from threading import Event, Thread

import pytest
import tomlkit

from omnibci.application.config_errors import ConfigProfileValidationError
from omnibci.application.plugin_profiles import ProfileRevisionConflict, SystemSettings
from omnibci.conf import OmniBCISettings
from omnibci.conf.storage import file_lock
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


def test_engine_and_backend_share_user_file_and_keep_independent_revisions(tmp_path):
    repository = TomlPluginProfileRepository(tmp_path)
    host = OmniBCISettings(user_file=tmp_path / "settings.toml")
    assert repository.get_system() is None
    initial = repository.save_system(SystemSettings({"NEUROENGINE.queue_size": 200, "debug": False}))
    assert not (tmp_path / "activation" / "system.toml").exists()
    assert host.read_saved()[0]["NEUROENGINE.queue_size"] == 200
    _, version = host.read_saved()
    host.update({"LOGGING.level": "DEBUG"}, expected_revision=version)
    assert repository.get_system() == initial
    changed = repository.save_system(SystemSettings({"NEUROENGINE.queue_size": 300}), initial.revision)
    assert host.read_saved()[0]["LOGGING.level"] == "DEBUG"
    assert host.read_saved()[0]["NEUROENGINE.queue_size"] == 300
    assert repository.get_system() == changed
    assert tomlkit.parse((tmp_path / "settings.toml").read_text()).unwrap() == {
        "engine": {"NEUROENGINE.queue_size": 300}, "LOGGING": {"level": "DEBUG"},
    }


def test_engine_rollback_and_removal_preserve_new_backend_values(tmp_path):
    repository = TomlPluginProfileRepository(tmp_path)
    host = OmniBCISettings(user_file=tmp_path / "settings.toml")
    previous = repository.save_system(SystemSettings({"debug": False}))
    candidate = repository.save_system(SystemSettings({"debug": True}), previous.revision)
    host.update({"NEUROENGINE.command_timeout_s": 25.0}, expected_revision=host.read_saved()[1])
    repository.save_system(previous, candidate.revision)
    assert host.read_saved()[0]["NEUROENGINE.command_timeout_s"] == 25.0
    assert repository.get_system() == previous
    repository.remove_system(expected_revision=previous.revision)
    assert repository.get_system() is None
    assert tomlkit.parse((tmp_path / "settings.toml").read_text()).unwrap() == {
        "NEUROENGINE": {"command_timeout_s": 25.0},
    }


def test_empty_accepted_engine_is_distinct_from_absence(tmp_path):
    repository = TomlPluginProfileRepository(tmp_path)
    value = repository.save_system(SystemSettings({}))
    assert repository.get_system() == value
    repository.remove_system(expected_revision=value.revision)
    assert repository.get_system() is None
    with pytest.raises(ProfileRevisionConflict):
        repository.remove_system(expected_revision=value.revision)


def test_backend_writer_cannot_bypass_engine_application(tmp_path):
    host = OmniBCISettings(user_file=tmp_path / "settings.toml")
    before, revision = host.read_saved()
    with pytest.raises(ConfigProfileValidationError, match="Engine"):
        host.update({"NEUROENGINE.queue_size": 300}, expected_revision=revision)
    assert host.read_saved() == (before, revision)
    assert not (tmp_path / "settings.toml").exists()


def test_backend_save_after_engine_apply_keeps_both_changes(tmp_path):
    repository = TomlPluginProfileRepository(tmp_path)
    host = OmniBCISettings(user_file=tmp_path / "settings.toml")
    previous = repository.save_system(SystemSettings({"debug": False}))
    _, backend_revision = host.read_saved()
    current = repository.save_system(SystemSettings({"debug": True}), previous.revision)
    host.update({"LOGGING.level": "WARNING"}, expected_revision=backend_revision)
    assert repository.get_system() == current
    assert host.read_saved()[0]["LOGGING.level"] == "WARNING"


def test_shared_file_cannot_redirect_engine_writes_through_symlink(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text('[LOGGING]\nlevel = "WARNING"\n')
    root = tmp_path / "config"
    root.mkdir()
    (root / "settings.toml").symlink_to(outside)
    repository = TomlPluginProfileRepository(root)
    with pytest.raises(ConfigProfileValidationError, match="symlink"):
        repository.save_system(SystemSettings({"debug": True}))
    assert outside.read_text() == '[LOGGING]\nlevel = "WARNING"\n'


def test_broken_user_settings_symlink_is_rejected_on_read(tmp_path):
    (tmp_path / "settings.toml").symlink_to(tmp_path / "missing.toml")
    with pytest.raises(ConfigProfileValidationError, match="symlink"):
        TomlPluginProfileRepository(tmp_path).get_system()


def test_invalid_backend_value_does_not_change_saved_settings(tmp_path):
    host = OmniBCISettings(user_file=tmp_path / "settings.toml")
    before, revision = host.read_saved()
    with pytest.raises(ConfigProfileValidationError, match="Invalid backend settings"):
        host.update({"NEUROENGINE.command_timeout_s": float("nan")}, expected_revision=revision)
    assert host.read_saved() == (before, revision)
    assert not (tmp_path / "settings.toml").exists()


def test_invalid_engine_file_uses_the_host_validation_error(tmp_path):
    path = tmp_path / "settings.toml"
    content = '[engine]\n"NEUROENGINE.queue_size" = 0\n'
    path.write_text(content)
    host = OmniBCISettings(user_file=path)
    for read in (host.read_saved, host.get_system):
        with pytest.raises(ConfigProfileValidationError, match="Invalid Engine settings"):
            read()
    assert path.read_text() == content


def test_engine_write_uses_host_lock_and_merges_latest_backend_document(tmp_path):
    repository = TomlPluginProfileRepository(tmp_path)
    previous = repository.save_system(SystemSettings({"debug": False}))
    started, finished = Event(), Event()
    errors = []
    def apply():
        started.set()
        try:
            repository.save_system(SystemSettings({"debug": True}), previous.revision)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    thread = Thread(target=apply)
    with file_lock(tmp_path / ".host-settings.lock"):
        thread.start()
        assert started.wait(1)
        assert not finished.wait(.05)
        path = tmp_path / "settings.toml"
        document = tomlkit.parse(path.read_text())
        document["LOGGING"] = {"level": "WARNING"}
        path.write_text(tomlkit.dumps(document))
    thread.join(2)
    assert finished.is_set() and not errors
    assert OmniBCISettings(user_file=tmp_path / "settings.toml").read_saved()[0]["LOGGING.level"] == "WARNING"
    assert repository.get_system().settings == {"debug": True}


def test_failed_engine_write_preserves_file_and_stale_rollback_is_rejected(tmp_path, monkeypatch):
    from omnibci.conf import storage
    repository = TomlPluginProfileRepository(tmp_path)
    previous = repository.save_system(SystemSettings({"debug": False}))
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    def fail_replace(*args):
        raise OSError("write failed")
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="write failed"):
        repository.save_system(SystemSettings({"debug": True}), previous.revision)
    assert path.read_bytes() == before
    with pytest.raises(ProfileRevisionConflict):
        repository.save_system(SystemSettings({"debug": True}), "0" * 64)
    assert path.read_bytes() == before


def test_legacy_system_file_is_not_an_accepted_source(tmp_path):
    old = tmp_path / "activation" / "system.toml"
    old.parent.mkdir()
    old.write_text(tomlkit.dumps(SystemSettings({"debug": True}).to_dict()))
    repository = TomlPluginProfileRepository(tmp_path)
    assert repository.get_system() is None
    assert not (tmp_path / "settings.toml").exists()
