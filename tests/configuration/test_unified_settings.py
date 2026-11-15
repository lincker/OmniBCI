"""Engine acceptance and Server writes share one user file without lost updates."""
import multiprocessing
from pathlib import Path
from threading import Event, Thread

import pytest
import tomlkit

from omnibci.application.configuration.definitions import SERVER_STARTUP_DEFAULTS
from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError, ConfigProfileValidationError, ConfigValidationError,
    PluginProfileStorageError, ProfileRevisionConflict,
)
from omnibci.application.configuration.models import EngineSettings
from omnibci.infrastructure.repositories.configuration_storage import file_lock
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository


def test_engine_and_server_share_user_file_and_keep_independent_revisions(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    assert repository.get_engine() is None
    initial = repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 200, "debug": False}))
    assert not (tmp_path / "activation" / "system.toml").exists()
    _, version = repository.read_server()
    repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=version)
    assert repository.get_engine() == initial
    changed = repository.save_engine(EngineSettings({"NEUROENGINE.queue_size": 300}), initial.revision)
    assert repository.read_server()[0]["LOGGING.level"] == "DEBUG"
    assert repository.get_engine() == changed
    assert tomlkit.parse((tmp_path / "settings.toml").read_text()).unwrap() == {
        "engine": {"NEUROENGINE.queue_size": 300}, "LOGGING": {"level": "DEBUG"},
    }


def test_engine_rollback_and_removal_preserve_new_server_values(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    previous = repository.save_engine(EngineSettings({"debug": False}))
    candidate = repository.save_engine(EngineSettings({"debug": True}), previous.revision)
    repository.save_server({"NEUROENGINE.command_timeout_s": 25.0}, expected_revision=repository.read_server()[1])
    repository.save_engine(previous, candidate.revision)
    assert repository.read_server()[0]["NEUROENGINE.command_timeout_s"] == 25.0
    assert repository.get_engine() == previous
    repository.remove_engine(expected_revision=previous.revision)
    assert repository.get_engine() is None
    assert tomlkit.parse((tmp_path / "settings.toml").read_text()).unwrap() == {
        "NEUROENGINE": {"command_timeout_s": 25.0},
    }


def test_empty_accepted_engine_is_distinct_from_absence(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    value = repository.save_engine(EngineSettings({}))
    assert repository.get_engine() == value
    repository.remove_engine(expected_revision=value.revision)
    assert repository.get_engine() is None
    with pytest.raises(ProfileRevisionConflict):
        repository.remove_engine(expected_revision=value.revision)


def test_server_writer_cannot_bypass_engine_application(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    before, revision = repository.read_server()
    with pytest.raises(ConfigKeyNotAllowedError):
        repository.save_server({"NEUROENGINE.queue_size": 300}, expected_revision=revision)
    assert repository.read_server() == (before, revision)
    assert not (tmp_path / "settings.toml").exists()


def test_server_save_after_engine_apply_keeps_both_changes(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    previous = repository.save_engine(EngineSettings({"debug": False}))
    _, server_revision = repository.read_server()
    current = repository.save_engine(EngineSettings({"debug": True}), previous.revision)
    repository.save_server({"LOGGING.level": "WARNING"}, expected_revision=server_revision)
    assert repository.get_engine() == current
    assert repository.read_server()[0]["LOGGING.level"] == "WARNING"


def test_shared_file_cannot_redirect_engine_writes_through_symlink(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text('[LOGGING]\nlevel = "WARNING"\n')
    root = tmp_path / "config"
    root.mkdir()
    (root / "settings.toml").symlink_to(outside)
    repository = TomlConfigurationRepository(root)
    with pytest.raises(ConfigProfileValidationError, match="symlink"):
        repository.save_engine(EngineSettings({"debug": True}))
    assert outside.read_text() == '[LOGGING]\nlevel = "WARNING"\n'


def test_broken_user_settings_symlink_is_rejected_on_read(tmp_path):
    (tmp_path / "settings.toml").symlink_to(tmp_path / "missing.toml")
    repository = TomlConfigurationRepository(tmp_path)
    for read in (repository.read_server, repository.get_engine):
        with pytest.raises(ConfigProfileValidationError, match="symlink"):
            read()


@pytest.mark.parametrize("target", ["locks", "locks/settings.lock"])
def test_settings_lock_symlinks_use_the_settings_validation_error(tmp_path, target):
    root = tmp_path / "configuration"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    path = root / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(outside, target_is_directory=True)
    repository = TomlConfigurationRepository(root)
    for read in (repository.read_server, repository.get_engine):
        with pytest.raises(ConfigProfileValidationError):
            read()
    assert list(outside.iterdir()) == []


def test_invalid_server_value_does_not_change_saved_settings(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    before, revision = repository.read_server()
    with pytest.raises(ConfigValidationError):
        repository.save_server({"NEUROENGINE.command_timeout_s": float("nan")}, expected_revision=revision)
    assert repository.read_server() == (before, revision)
    assert not (tmp_path / "settings.toml").exists()


def test_invalid_engine_does_not_block_server_reads_and_writes(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[engine]\n"NEUROENGINE.queue_size" = 0\n')
    repository = TomlConfigurationRepository(tmp_path)
    values, revision = repository.read_server()
    assert values == SERVER_STARTUP_DEFAULTS
    repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    assert repository.read_server()[0]["LOGGING.level"] == "DEBUG"
    assert tomlkit.parse(path.read_text())["engine"]["NEUROENGINE.queue_size"] == 0
    with pytest.raises(ConfigProfileValidationError, match="Invalid Engine settings"):
        repository.get_engine()


def test_invalid_server_does_not_block_engine_reads_and_writes(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\ncommand_timeout_s = "invalid"\n')
    repository = TomlConfigurationRepository(tmp_path)
    assert repository.get_engine() is None
    saved = repository.save_engine(EngineSettings({"debug": True}))
    assert repository.get_engine() == saved
    assert tomlkit.parse(path.read_text())["NEUROENGINE"]["command_timeout_s"] == "invalid"
    with pytest.raises(ConfigProfileValidationError, match="Invalid Server settings"):
        repository.read_server()


def test_invalid_module_does_not_block_server_reads_and_writes(tmp_path):
    path = tmp_path / "activation" / "stimulation.toml"
    path.parent.mkdir()
    path.write_text("invalid = [")
    repository = TomlConfigurationRepository(tmp_path)
    _, revision = repository.read_server()
    repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    assert repository.read_server()[0]["LOGGING.level"] == "DEBUG"
    assert path.read_text() == "invalid = ["


def test_engine_write_uses_shared_lock_and_merges_latest_server_document(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    previous = repository.save_engine(EngineSettings({"debug": False}))
    started, finished = Event(), Event()
    errors = []
    def apply():
        started.set()
        try:
            repository.save_engine(EngineSettings({"debug": True}), previous.revision)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()
    thread = Thread(target=apply)
    with file_lock(tmp_path / "locks" / "settings.lock"):
        thread.start()
        assert started.wait(1)
        assert not finished.wait(.05)
        path = tmp_path / "settings.toml"
        document = tomlkit.parse(path.read_text())
        document["LOGGING"] = {"level": "WARNING"}
        path.write_text(tomlkit.dumps(document))
    thread.join(2)
    assert finished.is_set() and not errors
    assert repository.read_server()[0]["LOGGING.level"] == "WARNING"
    assert repository.get_engine().settings == {"debug": True}


def test_failed_engine_write_preserves_file_and_stale_rollback_is_rejected(tmp_path, monkeypatch):
    from omnibci.infrastructure.repositories import configuration_storage as storage
    repository = TomlConfigurationRepository(tmp_path)
    previous = repository.save_engine(EngineSettings({"debug": False}))
    path = tmp_path / "settings.toml"
    before = path.read_bytes()
    def fail_replace(*args):
        raise OSError("write failed")
    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(PluginProfileStorageError, match="write failed"):
        repository.save_engine(EngineSettings({"debug": True}), previous.revision)
    assert path.read_bytes() == before
    with pytest.raises(ProfileRevisionConflict):
        repository.save_engine(EngineSettings({"debug": True}), "0" * 64)
    assert path.read_bytes() == before


def test_removed_engine_file_is_not_an_accepted_source(tmp_path):
    old = tmp_path / "activation" / "system.toml"
    old.parent.mkdir()
    old.write_text(tomlkit.dumps(EngineSettings({"debug": True}).to_dict()))
    repository = TomlConfigurationRepository(tmp_path)
    assert repository.get_engine() is None
    assert not (tmp_path / "settings.toml").exists()


def _save_section(root, section, revision, ready, start, results):
    repository = TomlConfigurationRepository(Path(root))
    ready.put(section)
    if not start.wait(15):
        results.put((section, "timeout"))
        return
    try:
        if section == "server":
            repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
        else:
            repository.save_engine(EngineSettings({"debug": True}))
    except Exception as exc:
        results.put((section, str(exc)))
    else:
        results.put((section, "saved"))


def test_concurrent_server_and_engine_processes_keep_both_changes(tmp_path):
    repository = TomlConfigurationRepository(tmp_path)
    _, revision = repository.read_server()
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [context.Process(target=_save_section, args=(
        str(tmp_path), section, revision, ready, start, results,
    )) for section in ("server", "engine")]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=20), ready.get(timeout=20)} == {"server", "engine"}
        start.set()
        assert {results.get(timeout=20), results.get(timeout=20)} == {("server", "saved"), ("engine", "saved")}
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert repository.read_server()[0]["LOGGING.level"] == "DEBUG"
        assert repository.get_engine().settings == {"debug": True}
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        ready.close()
        results.close()
