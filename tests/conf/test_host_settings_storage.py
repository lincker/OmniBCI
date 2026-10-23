"""Saved host overrides are portable, revisioned, and isolated from workers."""

import multiprocessing
import os
from pathlib import Path
import re

import pytest
import tomlkit

from omnibci.application.config_errors import (
    ConfigProfileConflictError,
    ConfigProfileValidationError,
)
from omnibci.conf import OmniBCISettings


@pytest.fixture
def host_files(tmp_path, monkeypatch):
    defaults = tmp_path / "bundled" / "settings.toml"
    defaults.parent.mkdir()
    defaults.write_text(
        '# Bundled defaults remain read-only.\ndebug = false\n'
        '[NEUROENGINE]\nqueue_size = 100\ncommand_timeout_s = 10.0\n'
        '[LOGGING]\nlevel = "INFO"\nfile_path = "omnibci.log"\n',
        encoding="utf-8",
    )
    user_root = tmp_path / "user-config"
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(user_root))
    return defaults, user_root / "settings.toml"


def test_fresh_read_uses_defaults_without_creating_user_settings(host_files):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)

    values, revision = settings.read_saved()

    assert values["NEUROENGINE.queue_size"] == 100
    assert values["LOGGING.level"] == "INFO"
    assert re.fullmatch(r"[0-9a-f]{64}", revision)
    assert settings.read_saved() == (values, revision)
    assert not user_file.exists()


def test_user_overrides_win_and_only_overrides_are_written(host_files):
    defaults, user_file = host_files
    original = defaults.read_bytes()
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()

    saved_revision = settings.update(
        {"NEUROENGINE.queue_size": 250, "LOGGING.level": "DEBUG"},
        expected_revision=revision,
    )

    assert saved_revision != revision
    assert defaults.read_bytes() == original
    assert tomlkit.parse(user_file.read_text(encoding="utf-8")).unwrap() == {
        "NEUROENGINE": {"queue_size": 250}, "LOGGING": {"level": "DEBUG"},
    }
    assert settings.snapshot()["NEUROENGINE.queue_size"] == 250
    restarted = OmniBCISettings(defaults_file=defaults)
    assert restarted.get("NEUROENGINE.queue_size") == 250
    assert restarted.get("NEUROENGINE.command_timeout_s") == 10.0
    assert restarted.read_saved()[1] == saved_revision


def test_retired_signal_setup_remains_readable_without_accepting_new_writes(host_files):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    setup = {
        "schema_version": 1, "source_id": "test-source", "template": "standard_1020",
        "assignments": [
            {"source_index": 0, "electrode": "O1", "expected_name": "CH1"},
            {"source_index": 1, "electrode": "O2", "expected_name": "CH2"},
        ],
        "outputs": [{"name": "O1-O2", "positive": "O1", "negative": "O2"}],
        "reference": {"mode": "bipolar"}, "original_reference": "Cz",
        "manual_metadata": {
            "source_id": "test-source", "sample_rate": 250, "trigger_row_index": 2,
            "channels": [{"index": 0, "name": "CH1", "unit": "uV"}, {"index": 1, "name": "CH2", "unit": "uV"}],
        },
    }
    user_file.parent.mkdir(exist_ok=True)
    user_file.write_text(tomlkit.dumps({"SIGNAL": {"setup": setup}}), encoding="utf-8")
    with pytest.raises(ConfigProfileValidationError, match="SIGNAL.setup"):
        settings.update({"SIGNAL.setup": setup}, expected_revision=revision)
    restarted = OmniBCISettings(defaults_file=defaults)

    assert restarted.read_saved()[0]["SIGNAL.setup"] == setup
    assert restarted.get("SIGNAL.setup") == setup
    assert not any(key.startswith("SIGNAL.setup.") for key in restarted.snapshot())
    assert tomlkit.parse(user_file.read_text(encoding="utf-8")).unwrap()["SIGNAL"]["setup"] == setup

    revision = restarted.read_saved()[1]
    revision = restarted.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)
    assert restarted.get("SIGNAL.setup") == setup
    with pytest.raises(ConfigProfileValidationError, match="SIGNAL.setup"):
        restarted.update({"SIGNAL.setup": {"outputs": []}}, expected_revision=revision)
    assert OmniBCISettings(defaults_file=defaults).get("SIGNAL.setup") == setup


def test_retired_channel_names_are_ignored_without_rewriting_saved_settings(host_files):
    from omnibci.application.activation_settings import activation_host_settings
    from omnibci.infrastructure.plugins.runtime_configuration import plugin_context

    defaults, user_file = host_files
    user_file.parent.mkdir()
    content = '[ACQUISITION]\nchannel_names = ["Stale", "Trigger"]\n'
    user_file.write_text(content, encoding="utf-8")
    settings = OmniBCISettings(defaults_file=defaults)
    saved, revision = settings.read_saved()

    assert "ACQUISITION.channel_names" not in activation_host_settings(saved)
    assert plugin_context(saved).channel_names == ()
    with pytest.raises(ConfigProfileValidationError, match="ACQUISITION.channel_names"):
        settings.update({"ACQUISITION.channel_names": ["O1", "O2"]}, expected_revision=revision)
    assert settings.read_saved() == (saved, revision)
    assert user_file.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_saved_revision_matches_disk_with_windows_text_writes(host_files, monkeypatch, line_ending):
    from omnibci.conf import storage

    temporary_file = storage.tempfile.NamedTemporaryFile

    def windows_temporary_file(*args, **kwargs):
        if kwargs.get("newline") is None:
            kwargs["newline"] = "\r\n"
        return temporary_file(*args, **kwargs)

    monkeypatch.setattr(storage.tempfile, "NamedTemporaryFile", windows_temporary_file)
    defaults, user_file = host_files
    user_file.parent.mkdir()
    content = f'# Keep the café comment.{line_ending}[NEUROENGINE]{line_ending}queue_size = 100{line_ending}'
    user_file.write_bytes(content.encode("utf-8"))
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()

    for queue_size in (250, 300):
        revision = settings.update({"NEUROENGINE.queue_size": queue_size}, expected_revision=revision)
        assert settings.read_saved()[1] == revision
        assert user_file.read_bytes() == content.replace("100", str(queue_size)).encode("utf-8")
        assert OmniBCISettings(defaults_file=defaults).read_saved()[1] == revision


def test_existing_user_override_is_merged_with_bundled_defaults(host_files):
    defaults, user_file = host_files
    user_file.parent.mkdir()
    user_file.write_text('[LOGGING]\nlevel = "WARNING"\n', encoding="utf-8")

    values, _ = OmniBCISettings(defaults_file=defaults).read_saved()

    assert values["LOGGING.level"] == "WARNING"
    assert values["LOGGING.file_path"] == "omnibci.log"
    assert values["NEUROENGINE.queue_size"] == 100


def test_explicit_file_is_independent_of_user_override(host_files, tmp_path):
    _, user_file = host_files
    user_file.parent.mkdir()
    user_file.write_text('[NEUROENGINE]\nqueue_size = 400\n', encoding="utf-8")
    explicit = tmp_path / "explicit.toml"
    explicit.write_text('[NEUROENGINE]\nqueue_size = 50\n', encoding="utf-8")

    settings = OmniBCISettings(toml_file=explicit)

    assert settings.read_saved()[0] == {"NEUROENGINE.queue_size": 50}
    _, revision = settings.read_saved()
    settings.update({"NEUROENGINE.queue_size": 60}, expected_revision=revision)
    assert OmniBCISettings(toml_file=explicit).get("NEUROENGINE.queue_size") == 60
    assert '400' in user_file.read_text(encoding="utf-8")


def test_malformed_user_file_is_not_replaced_with_defaults(host_files):
    defaults, user_file = host_files
    user_file.parent.mkdir()
    broken = '[NEUROENGINE\nqueue_size = 250\n'
    user_file.write_text(broken, encoding="utf-8")

    with pytest.raises(ConfigProfileValidationError):
        OmniBCISettings(defaults_file=defaults).read_saved()

    assert user_file.read_text(encoding="utf-8") == broken


def test_user_settings_path_cannot_overwrite_bundled_defaults(host_files, monkeypatch):
    defaults, _ = host_files
    original = defaults.read_bytes()
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(defaults.parent))

    with pytest.raises(ConfigProfileValidationError):
        settings = OmniBCISettings(defaults_file=defaults)
        _, revision = settings.read_saved()
        settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)

    assert defaults.read_bytes() == original


def test_invalid_utf8_is_reported_without_falling_back_or_overwriting(host_files):
    defaults, user_file = host_files
    user_file.parent.mkdir()
    original = b'[LOGGING]\nlevel = "\xff"\n'
    user_file.write_bytes(original)

    with pytest.raises(ConfigProfileValidationError):
        OmniBCISettings(defaults_file=defaults).read_saved()

    assert user_file.read_bytes() == original


@pytest.mark.parametrize("document", [
    '[ACQUISITION]\nsample_rate = 500\n',
    '[ALGORITHM]\ndecoder = "custom_decoder"\n',
    '[PLUGINS]\nbindings = []\n',
    '[CONFIG]\nrevision = "injected"\n',
    '[UNREVIEWED]\nvalue = 1\n',
])
def test_user_overlay_rejects_plugin_generated_and_unknown_fields(host_files, document):
    defaults, user_file = host_files
    user_file.parent.mkdir()
    user_file.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigProfileValidationError):
        OmniBCISettings(defaults_file=defaults).read_saved()

    assert user_file.read_text(encoding="utf-8") == document


@pytest.mark.parametrize("key", [
    "ACQUISITION.sample_rate", "ALGORITHM.decoder", "PLUGINS.bindings",
    "CONFIG.revision", "UNREVIEWED.value",
])
def test_update_rejects_non_host_fields_without_persisting(host_files, key):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    before, revision = settings.read_saved()

    with pytest.raises(ConfigProfileValidationError):
        settings.update({key: 1}, expected_revision=revision)

    assert settings.read_saved() == (before, revision)
    assert not user_file.exists()


@pytest.mark.parametrize("revision", [None, "", "invalid", "f" * 63, "g" * 64])
def test_update_requires_a_well_formed_revision(host_files, revision):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)

    with pytest.raises(ConfigProfileValidationError):
        settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)

    assert not user_file.exists()


def test_stale_instance_cannot_overwrite_another_saved_update(host_files):
    defaults, _ = host_files
    first = OmniBCISettings(defaults_file=defaults)
    second = OmniBCISettings(defaults_file=defaults)
    _, revision = first.read_saved()
    assert second.read_saved()[1] == revision

    first.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)
    with pytest.raises(ConfigProfileConflictError):
        second.update({"LOGGING.level": "DEBUG"}, expected_revision=revision)

    saved = second.saved_snapshot()
    assert saved["NEUROENGINE.queue_size"] == 250
    assert saved["LOGGING.level"] == "INFO"


def test_external_edit_invalidates_previous_revision(host_files):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    revision = settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)
    user_file.write_text('[NEUROENGINE]\nqueue_size = 300\n', encoding="utf-8")

    with pytest.raises(ConfigProfileConflictError):
        settings.update({"LOGGING.level": "DEBUG"}, expected_revision=revision)

    assert settings.saved_snapshot()["NEUROENGINE.queue_size"] == 300


@pytest.mark.parametrize("edited_file", ["defaults", "user"])
def test_comment_only_external_edit_invalidates_previous_revision(host_files, edited_file):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    revision = settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)
    target = defaults if edited_file == "defaults" else user_file
    with target.open("a", encoding="utf-8") as stream:
        stream.write("\n# An external edit must not be overwritten.\n")

    with pytest.raises(ConfigProfileConflictError):
        settings.update({"LOGGING.level": "DEBUG"}, expected_revision=revision)

    values, refreshed_revision = settings.read_saved()
    assert refreshed_revision != revision
    assert values["NEUROENGINE.queue_size"] == 250


def test_deleted_user_file_invalidates_previous_revision(host_files):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    revision = settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)
    user_file.unlink()

    with pytest.raises(ConfigProfileConflictError):
        settings.update({"LOGGING.level": "DEBUG"}, expected_revision=revision)

    assert settings.saved_snapshot()["NEUROENGINE.queue_size"] == 100
    assert not user_file.exists()


def test_saved_read_is_refreshed_but_cached_reads_do_not_touch_disk(host_files, monkeypatch):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    settings.read_saved()
    user_file.parent.mkdir(exist_ok=True)
    user_file.write_text('[NEUROENGINE]\nqueue_size = 300\n', encoding="utf-8")
    assert settings.saved_snapshot()["NEUROENGINE.queue_size"] == 300

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Cached settings reads must not open a file")

    monkeypatch.setattr(Path, "open", unexpected_read)
    assert settings.get("NEUROENGINE.queue_size") == 300
    assert settings.snapshot()["NEUROENGINE.queue_size"] == 300


def test_failed_replace_preserves_disk_and_cached_settings(host_files, monkeypatch):
    from omnibci.conf import storage

    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    revision = settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)
    before_bytes = user_file.read_bytes()
    before_cache = settings.snapshot()
    before_files = set(user_file.parent.iterdir())

    def failed_replace(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        settings.update({"NEUROENGINE.queue_size": 300}, expected_revision=revision)

    assert user_file.read_bytes() == before_bytes
    assert settings.snapshot() == before_cache
    assert settings.read_saved()[1] == revision
    assert set(user_file.parent.iterdir()) == before_files


def test_failed_first_write_does_not_publish_an_override(host_files, monkeypatch):
    from omnibci.conf import storage

    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    values, revision = settings.read_saved()

    def failed_replace(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
        settings.update({"NEUROENGINE.queue_size": 300}, expected_revision=revision)

    assert not user_file.exists()
    assert settings.snapshot() == values
    assert settings.read_saved()[1] == revision


def test_worker_snapshot_never_reads_files_or_falls_back_to_host(host_files, monkeypatch):
    import omnibci.conf as conf

    defaults, _ = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    worker = {"NEUROENGINE.queue_size": 50, "PLUGINS.bindings": [{"parameters": {"size": 3}}]}
    settings.use_snapshot(worker)
    worker["PLUGINS.bindings"][0]["parameters"]["size"] = 7

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Worker settings reads must not open a file")

    class UnexpectedLock:
        def __enter__(self):
            raise AssertionError("Worker settings reads must not acquire a host lock")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(Path, "open", unexpected_read)
    monkeypatch.setattr(conf, "file_lock", unexpected_read)
    monkeypatch.setattr(settings, "_lock", UnexpectedLock())
    assert settings.get("NEUROENGINE.queue_size") == 50
    assert settings.get("LOGGING.level", None) is None
    with pytest.raises(NameError, match="LOGGING.level"):
        settings.get("LOGGING.level")
    returned = settings.snapshot()
    returned["PLUGINS.bindings"][0]["parameters"]["size"] = 9
    assert settings.snapshot()["PLUGINS.bindings"][0]["parameters"]["size"] == 3


def test_worker_can_install_snapshot_before_missing_host_defaults_are_loaded(tmp_path):
    settings = OmniBCISettings(defaults_file=tmp_path / "missing-defaults.toml")

    settings.use_snapshot({"NEUROENGINE.queue_size": 50})

    assert settings.get("NEUROENGINE.queue_size") == 50
    assert settings.snapshot() == {"NEUROENGINE.queue_size": 50}


def test_explicit_saved_management_does_not_replace_installed_worker_snapshot(host_files):
    defaults, _ = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    worker = {"NEUROENGINE.queue_size": 50}
    settings.use_snapshot(worker)

    settings.update({"NEUROENGINE.queue_size": 250}, expected_revision=revision)

    assert settings.saved_snapshot()["NEUROENGINE.queue_size"] == 250
    assert settings.read_saved()[0]["NEUROENGINE.queue_size"] == 250
    assert settings.snapshot() == worker
    assert settings.get("NEUROENGINE.queue_size") == 50


def _competing_update(defaults, user_root, revision, queue_size, ready, start, results):
    """Spawn-safe writer using the same revision captured by the parent."""
    os.environ["OMNIBCI_CONFIG_DIR"] = user_root
    settings = OmniBCISettings(defaults_file=defaults)
    ready.put(queue_size)
    if not start.wait(15):
        results.put(("timeout", queue_size))
        return
    try:
        settings.update({"NEUROENGINE.queue_size": queue_size}, expected_revision=revision)
    except ConfigProfileConflictError:
        results.put(("conflict", queue_size))
    except Exception as exc:
        results.put((type(exc).__name__, str(exc)))
    else:
        results.put(("saved", queue_size))


def test_competing_processes_publish_exactly_one_update(host_files):
    defaults, user_file = host_files
    settings = OmniBCISettings(defaults_file=defaults)
    _, revision = settings.read_saved()
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    start = context.Event()
    processes = [context.Process(target=_competing_update, args=(
        str(defaults), str(user_file.parent), revision, queue_size, ready, start, results,
    )) for queue_size in (250, 300)]

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
        assert settings.saved_snapshot()["NEUROENGINE.queue_size"] == winner
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        ready.close()
        results.close()
