"""Server settings use strict, portable, revisioned writes without cached state."""

import multiprocessing
from pathlib import Path
import re

import pytest
import tomlkit

from omnibci.application.configuration.definitions import SERVER_STARTUP_DEFAULTS
from omnibci.application.configuration.errors import (
    ConfigKeyNotAllowedError, ConfigProfileConflictError, ConfigProfileValidationError,
    ConfigValidationError, PluginProfileValidationError,
)
from omnibci.infrastructure.repositories.configuration_repository import TomlConfigurationRepository
from omnibci.infrastructure import storage


@pytest.fixture
def repository(tmp_path):
    return TomlConfigurationRepository(tmp_path / "configuration")


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
    '[SIGNAL]\nsetup = {}\n',
    '[ACQUISITION]\nchannel_names = ["Stale", "Trigger"]\n',
    '[ACQUISITION]\nsample_rate = 500\n',
    '[ALGORITHM]\ndecoder = "custom_decoder"\n',
    '[STIMULATION]\nrenderer = "moderngl"\n',
    '[PLUGINS]\nbindings = []\n',
    '[CONFIG]\nrevision = "injected"\n',
    '[UNREVIEWED]\n',
    '[PROFILES]\nactive = "old-profile"\n',
    'debug = true\n',
    '[NEUROENGINE]\nqueue_size = 200\n',
])
def test_retired_foreign_and_unknown_settings_are_rejected_without_rewriting(repository, content):
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
    "ACQUISITION.sample_rate", "ALGORITHM.decoder", "PLUGINS.bindings",
    "CONFIG.revision", "UNREVIEWED.value", "NEUROENGINE.queue_size",
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
