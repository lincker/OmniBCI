"""Server and Engine configuration storage uses strict, portable, revisioned TOML writes."""

import multiprocessing
from pathlib import Path
import re
from threading import Event, Thread

import pytest
import tomlkit

from omnibci.application.configuration.definitions import SERVER_STARTUP_DEFAULTS
from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError,
    ConfigProfileConflictError,
    ConfigProfileValidationError,
    ConfigValidationError,
    PluginProfileStorageError,
    PluginProfileValidationError,
    ProfileRevisionConflict,
)
from omnibci.application.configuration.models import EngineSettings
from omnibci.infrastructure import storage
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from omnibci.infrastructure.storage import file_lock


@pytest.fixture
def repository(tmp_path):
    return TomlConfigurationRepository(tmp_path / "configuration")


def test_server_defaults_are_declared_without_creating_files(tmp_path):
    root = tmp_path / "configuration"
    repo = TomlConfigurationRepository(root)
    values, revision = repo.read_server()
    assert values == SERVER_STARTUP_DEFAULTS
    assert repo.read_server() == (values, revision)
    assert repo.get_engine() is None
    assert not root.exists()


def test_server_reads_are_independent_values(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    values, revision = repo.read_server()
    values["LOGGING.level"] = "DEBUG"
    assert repo.read_server() == (SERVER_STARTUP_DEFAULTS, revision)


def test_module_and_engine_settings_are_not_in_server_values(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    repo.save_engine(EngineSettings({"NEUROENGINE.queue_size": 200, "debug": True}))
    assert repo.read_server()[0] == SERVER_STARTUP_DEFAULTS
    assert repo.get_engine().settings == {"NEUROENGINE.queue_size": 200, "debug": True}


def test_overrides_are_persisted_and_missing_fields_use_declared_defaults(repository):
    before, revision = repository.read_server()
    saved_revision = repository.save_server(
        {"NEUROENGINE.command_timeout_s": 250, "LOGGING.level": "DEBUG"}, expected_revision=revision,
    )
    assert before == SERVER_STARTUP_DEFAULTS
    assert saved_revision != revision
    assert re.fullmatch(r"[0-9a-f]{64}", saved_revision)
    path = repository.root / "settings.toml"
    assert tomlkit.parse(path.read_text()).unwrap() == {
        "NEUROENGINE": {"command_timeout_s": 250.0}, "LOGGING": {"level": "DEBUG"},
    }
    restarted = TomlConfigurationRepository(repository.root)
    assert restarted.read_server() == ({**before, "NEUROENGINE.command_timeout_s": 250.0,
                                       "LOGGING.level": "DEBUG"}, saved_revision)


@pytest.mark.parametrize("content", [
    '[UNREVIEWED]\n',
    'debug = true\n',
    '[NEUROENGINE]\nqueue_size = 200\n',
])
def test_foreign_and_unknown_settings_are_rejected_without_rewriting(repository, content):
    repository.root.mkdir()
    path = repository.root / "settings.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigProfileValidationError):
        repository.read_server()
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_revision_survives_windows_text_writes_and_preserves_comments(repository, monkeypatch, line_ending):
    temporary_file = storage.tempfile.NamedTemporaryFile

    def windows_temporary_file(*args, **kwargs):
        if kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return temporary_file(*args, **kwargs)

    monkeypatch.setattr(storage.tempfile, "NamedTemporaryFile", windows_temporary_file)
    repository.root.mkdir()
    path = repository.root / "settings.toml"
    path.write_bytes(f'# Keep the café comment.{line_ending}[NEUROENGINE]{line_ending}command_timeout_s = 100{line_ending}'.encode())
    revision = repository.read_server()[1]
    saved_revision = repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    assert TomlConfigurationRepository(repository.root).read_server()[1] == saved_revision
    assert "# Keep the café comment." in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("content", [b'[LOGGING\nlevel = "DEBUG"', b'\xff\xfeinvalid'])
def test_invalid_document_is_not_replaced_by_defaults(repository, content):
    repository.root.mkdir()
    path = repository.root / "settings.toml"
    path.write_bytes(content)
    with pytest.raises(ConfigProfileValidationError):
        repository.read_server()
    with pytest.raises(ConfigProfileValidationError):
        repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision="0" * 64)
    assert path.read_bytes() == content


@pytest.mark.parametrize("key", [
    "UNREVIEWED.value", "NEUROENGINE.queue_size",
])
def test_server_update_rejects_foreign_fields_without_writing(repository, key):
    before, revision = repository.read_server()
    with pytest.raises(ConfigKeyNotAllowedError):
        repository.save_server({key: 1}, expected_revision=revision)
    assert repository.read_server() == (before, revision)
    assert not repository.root.exists()


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "20"])
def test_invalid_server_values_are_rejected_at_the_store_boundary(repository, value):
    before, revision = repository.read_server()
    with pytest.raises(ConfigValidationError):
        repository.save_server({"NEUROENGINE.command_timeout_s": value}, expected_revision=revision)
    assert repository.read_server() == (before, revision)
    assert not repository.root.exists()


@pytest.mark.parametrize("revision", [None, "", "invalid", "f" * 63, "g" * 64])
def test_server_update_requires_a_well_formed_revision(repository, revision):
    with pytest.raises((ConfigProfileValidationError, PluginProfileValidationError)):
        repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    assert not repository.root.exists()


def test_stale_instance_cannot_overwrite_another_update(repository):
    second = TomlConfigurationRepository(repository.root)
    _, revision = repository.read_server()
    assert second.read_server()[1] == revision
    repository.save_server({"NEUROENGINE.command_timeout_s": 250}, expected_revision=revision)
    with pytest.raises(ConfigProfileConflictError):
        second.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    saved = second.read_server()[0]
    assert saved["NEUROENGINE.command_timeout_s"] == 250
    assert saved["LOGGING.level"] == "INFO"


def test_external_edit_invalidates_previous_revision(repository):
    _, revision = repository.read_server()
    revision = repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    path = repository.root / "settings.toml"
    path.write_text('[NEUROENGINE]\ncommand_timeout_s = 300\n', encoding="utf-8")
    with pytest.raises(ConfigProfileConflictError):
        repository.save_server({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert repository.read_server()[0]["NEUROENGINE.command_timeout_s"] == 300


def test_comment_only_edits_are_preserved_without_invalidating_server_values(repository):
    _, revision = repository.read_server()
    revision = repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    path = repository.root / "settings.toml"
    comment = "\n# Preserve an unrelated external edit.\n"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(comment)
    assert repository.read_server()[1] == revision
    repository.save_server({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert comment.strip() in path.read_text()


def test_deleted_file_invalidates_an_override_revision(repository):
    _, revision = repository.read_server()
    revision = repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    path = repository.root / "settings.toml"
    path.unlink()
    with pytest.raises(ConfigProfileConflictError):
        repository.save_server({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert repository.read_server()[0] == SERVER_STARTUP_DEFAULTS
    assert not path.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_failed_replace_preserves_saved_values_and_removes_temporary_file(repository, monkeypatch, existing):
    _, revision = repository.read_server()
    if existing:
        revision = repository.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    path = repository.root / "settings.toml"
    before = path.read_bytes() if existing else None
    values = repository.read_server()[0]

    def failed_replace(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        repository.save_server({"LOGGING.level": "WARNING"}, expected_revision=revision)
    assert (path.read_bytes() if path.exists() else None) == before
    assert repository.read_server() == (values, revision)
    assert not list(repository.root.glob(".settings.toml.*"))


def _competing_update(root, revision, timeout, ready, start, results):
    repository = TomlConfigurationRepository(Path(root))
    ready.put(timeout)
    if not start.wait(15):
        results.put(("timeout", timeout))
        return
    try:
        repository.save_server({"NEUROENGINE.command_timeout_s": timeout}, expected_revision=revision)
    except ConfigProfileConflictError:
        results.put(("conflict", timeout))
    except Exception as exc:
        results.put((type(exc).__name__, str(exc)))
    else:
        results.put(("saved", timeout))


def test_competing_processes_publish_exactly_one_update(repository):
    _, revision = repository.read_server()
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [context.Process(target=_competing_update, args=(
        str(repository.root), revision, timeout, ready, start, results,
    )) for timeout in (250, 300)]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=20), ready.get(timeout=20)} == {250, 300}
        start.set()
        outcomes = [results.get(timeout=20), results.get(timeout=20)]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(outcome[0] for outcome in outcomes) == ["conflict", "saved"]
        winner = next(value for status, value in outcomes if status == "saved")
        assert repository.read_server()[0]["NEUROENGINE.command_timeout_s"] == winner
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        ready.close()
        results.close()


def test_engine_and_server_share_user_file_and_keep_independent_revisions(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    assert repo.get_engine() is None
    initial = repo.save_engine(EngineSettings({"NEUROENGINE.queue_size": 200, "debug": False}))
    assert not (tmp_path / "activation" / "system.toml").exists()
    _, version = repo.read_server()
    repo.save_server({"LOGGING.level": "DEBUG"}, expected_revision=version)
    assert repo.get_engine() == initial
    changed = repo.save_engine(EngineSettings({"NEUROENGINE.queue_size": 300}), initial.revision)
    assert repo.read_server()[0]["LOGGING.level"] == "DEBUG"
    assert repo.get_engine() == changed
    assert tomlkit.parse((tmp_path / "settings.toml").read_text()).unwrap() == {
        "engine": {"NEUROENGINE.queue_size": 300}, "LOGGING": {"level": "DEBUG"},
    }


def test_engine_rollback_and_removal_preserve_new_server_values(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    previous = repo.save_engine(EngineSettings({"debug": False}))
    candidate = repo.save_engine(EngineSettings({"debug": True}), previous.revision)
    repo.save_server({"NEUROENGINE.command_timeout_s": 25.0}, expected_revision=repo.read_server()[1])
    repo.save_engine(previous, candidate.revision)
    assert repo.read_server()[0]["NEUROENGINE.command_timeout_s"] == 25.0
    assert repo.get_engine() == previous
    repo.remove_engine(expected_revision=previous.revision)
    assert repo.get_engine() is None
    assert tomlkit.parse((tmp_path / "settings.toml").read_text()).unwrap() == {
        "NEUROENGINE": {"command_timeout_s": 25.0},
    }


def test_empty_accepted_engine_is_distinct_from_absence(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    value = repo.save_engine(EngineSettings({}))
    assert repo.get_engine() == value
    repo.remove_engine(expected_revision=value.revision)
    assert repo.get_engine() is None
    with pytest.raises(ProfileRevisionConflict):
        repo.remove_engine(expected_revision=value.revision)


def test_server_save_after_engine_apply_keeps_both_changes(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    previous = repo.save_engine(EngineSettings({"debug": False}))
    _, server_revision = repo.read_server()
    current = repo.save_engine(EngineSettings({"debug": True}), previous.revision)
    repo.save_server({"LOGGING.level": "WARNING"}, expected_revision=server_revision)
    assert repo.get_engine() == current
    assert repo.read_server()[0]["LOGGING.level"] == "WARNING"


def test_shared_file_cannot_redirect_engine_writes_through_symlink(tmp_path):
    outside = tmp_path / "outside.toml"
    outside.write_text('[LOGGING]\nlevel = "WARNING"\n')
    root = tmp_path / "config"
    root.mkdir()
    (root / "settings.toml").symlink_to(outside)
    repo = TomlConfigurationRepository(root)
    with pytest.raises(ConfigProfileValidationError, match="symlink"):
        repo.save_engine(EngineSettings({"debug": True}))
    assert outside.read_text() == '[LOGGING]\nlevel = "WARNING"\n'


def test_broken_user_settings_symlink_is_rejected_on_read(tmp_path):
    (tmp_path / "settings.toml").symlink_to(tmp_path / "missing.toml")
    repo = TomlConfigurationRepository(tmp_path)
    for read in (repo.read_server, repo.get_engine):
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
    repo = TomlConfigurationRepository(root)
    for read in (repo.read_server, repo.get_engine):
        with pytest.raises(ConfigProfileValidationError):
            read()
    assert list(outside.iterdir()) == []


def test_invalid_engine_does_not_block_server_reads_and_writes(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[engine]\n"NEUROENGINE.queue_size" = 0\n')
    repo = TomlConfigurationRepository(tmp_path)
    values, revision = repo.read_server()
    assert values == SERVER_STARTUP_DEFAULTS
    repo.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    assert repo.read_server()[0]["LOGGING.level"] == "DEBUG"
    assert tomlkit.parse(path.read_text())["engine"]["NEUROENGINE.queue_size"] == 0
    with pytest.raises(ConfigProfileValidationError, match="Invalid Engine settings"):
        repo.get_engine()


def test_invalid_server_does_not_block_engine_reads_and_writes(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\ncommand_timeout_s = "invalid"\n')
    repo = TomlConfigurationRepository(tmp_path)
    assert repo.get_engine() is None
    saved = repo.save_engine(EngineSettings({"debug": True}))
    assert repo.get_engine() == saved
    assert tomlkit.parse(path.read_text())["NEUROENGINE"]["command_timeout_s"] == "invalid"
    with pytest.raises(ConfigProfileValidationError, match="Invalid Server settings"):
        repo.read_server()


def test_invalid_module_does_not_block_server_reads_and_writes(tmp_path):
    path = tmp_path / "activation" / "stimulation.toml"
    path.parent.mkdir()
    path.write_text("invalid = [")
    repo = TomlConfigurationRepository(tmp_path)
    _, revision = repo.read_server()
    repo.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
    assert repo.read_server()[0]["LOGGING.level"] == "DEBUG"
    assert path.read_text() == "invalid = ["


def test_engine_write_uses_shared_lock_and_merges_latest_server_document(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    previous = repo.save_engine(EngineSettings({"debug": False}))
    started, finished = Event(), Event()
    errors = []

    def apply():
        started.set()
        try:
            repo.save_engine(EngineSettings({"debug": True}), previous.revision)
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
    assert repo.read_server()[0]["LOGGING.level"] == "WARNING"
    assert repo.get_engine().settings == {"debug": True}


def test_failed_engine_write_preserves_file_and_stale_rollback_is_rejected(tmp_path, monkeypatch):
    repo = TomlConfigurationRepository(tmp_path)
    previous = repo.save_engine(EngineSettings({"debug": False}))
    path = tmp_path / "settings.toml"
    before = path.read_bytes()

    def fail_replace(*args):
        raise OSError("write failed")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(PluginProfileStorageError, match="write failed"):
        repo.save_engine(EngineSettings({"debug": True}), previous.revision)
    assert path.read_bytes() == before
    with pytest.raises(ProfileRevisionConflict):
        repo.save_engine(EngineSettings({"debug": True}), "0" * 64)
    assert path.read_bytes() == before


def _save_section(root, section, revision, ready, start, results):
    repo = TomlConfigurationRepository(Path(root))
    ready.put(section)
    if not start.wait(15):
        results.put((section, "timeout"))
        return
    try:
        if section == "server":
            repo.save_server({"LOGGING.level": "DEBUG"}, expected_revision=revision)
        else:
            repo.save_engine(EngineSettings({"debug": True}))
    except Exception as exc:
        results.put((section, str(exc)))
    else:
        results.put((section, "saved"))


def test_concurrent_server_and_engine_processes_keep_both_changes(tmp_path):
    repo = TomlConfigurationRepository(tmp_path)
    _, revision = repo.read_server()
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
        assert repo.read_server()[0]["LOGGING.level"] == "DEBUG"
        assert repo.get_engine().settings == {"debug": True}
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        ready.close()
        results.close()
