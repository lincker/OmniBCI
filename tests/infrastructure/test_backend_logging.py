"""File logging never formats or writes records on the producer thread."""

import logging
import os
from pathlib import Path
from threading import Event, Thread, current_thread
import time
import weakref

import pytest

from omnibci.infrastructure import backend_logging
from omnibci.infrastructure.backend_logging import BackendLogging


@pytest.fixture
def isolated_logging(monkeypatch, tmp_path):
    logger = logging.getLogger("omnibci")
    old = (logger.level, logger.propagate, logger.handlers[:], logger.disabled)
    logger.handlers = []
    logger.propagate = False
    logger.disabled = False
    monkeypatch.setattr(logging, "lastResort", None)
    monkeypatch.setattr(backend_logging, "user_log_path", lambda *a, **k: tmp_path)
    yield logger, tmp_path
    owner = BackendLogging._owner
    if owner is not None:
        owner.stop()
    logger.setLevel(old[0])
    logger.propagate, logger.handlers, logger.disabled = old[1:]


def test_level_traceback_location_and_existing_handlers(isolated_logging):
    logger, root = isolated_logging
    existing = logging.NullHandler()
    logger.addHandler(existing)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    with BackendLogging({"LOGGING.level": "WARNING", "LOGGING.file_path": "nested/app.log"}) as service:
        assert service.file_path == root / "nested" / f"app-backend-{os.getpid()}.log"
        assert existing in logger.handlers
        assert logger.propagate is True
        logger.info("filtered information")
        try:
            raise ValueError("readable error")
        except ValueError:
            logger.exception("operation %s", "failed")
    content = service.file_path.read_text()
    assert "filtered information" not in content
    assert "operation failed" in content
    assert "Traceback (most recent call last)" in content
    assert "ValueError: readable error" in content
    assert logger.handlers == [existing]
    assert logger.level == logging.DEBUG
    assert logger.propagate is True
    assert not service.is_running


def test_explicit_child_logger_level_cannot_bypass_file_filter(isolated_logging):
    child = logging.getLogger("omnibci.backend_logging_test_child")
    previous = child.level
    child.setLevel(logging.DEBUG)
    try:
        with BackendLogging({"LOGGING.level": "WARNING"}) as service:
            child.debug("filtered child debug")
            child.warning("included child warning")
    finally:
        child.setLevel(previous)
    content = service.file_path.read_text()
    assert "filtered child debug" not in content
    assert "included child warning" in content


def test_existing_last_resort_console_is_preserved(isolated_logging, monkeypatch, capsys):
    logger, _ = isolated_logging
    fallback = logging.StreamHandler()
    fallback.setLevel(logging.WARNING)
    monkeypatch.setattr(logging, "lastResort", fallback)
    with BackendLogging({}) as service:
        logger.warning("console warning")
        logger.info("file information")
    stderr = capsys.readouterr().err
    assert stderr.count("console warning") == 1
    assert "file information" not in stderr
    assert "file information" in service.file_path.read_text()
    assert logger.handlers == []
    assert logging.lastResort is fallback


def test_existing_console_chain_does_not_receive_an_extra_fallback(isolated_logging, monkeypatch, capsys):
    logger, _ = isolated_logging
    logger.propagate = True
    fallback = logging.StreamHandler()
    fallback.setLevel(logging.WARNING)
    monkeypatch.setattr(logging, "lastResort", fallback)
    console = logging.StreamHandler()
    root = logging.getLogger()
    root.addHandler(console)
    try:
        with BackendLogging({}):
            assert fallback not in logger.handlers
            logger.warning("single console warning")
    finally:
        root.removeHandler(console)
    assert capsys.readouterr().err.count("single console warning") == 1


def test_child_console_handler_suppresses_original_last_resort(isolated_logging, monkeypatch, capsys):
    fallback = logging.StreamHandler()
    fallback.setLevel(logging.WARNING)
    monkeypatch.setattr(logging, "lastResort", fallback)
    child = logging.getLogger("omnibci.backend_logging_child_console")
    console = logging.StreamHandler()
    child.addHandler(console)
    try:
        with BackendLogging({}) as service:
            child.warning("single child console warning")
    finally:
        child.removeHandler(console)
    assert capsys.readouterr().err.count("single child console warning") == 1
    assert service.file_path.read_text().count("single child console warning") == 1


@pytest.mark.parametrize("settings", [
    {"LOGGING.level": "VERBOSE"},
    {"LOGGING.level": 20},
    {"LOGGING.file_path": ""},
    {"LOGGING.file_path": "   "},
    {"LOGGING.file_path": None},
    {"LOGGING.file_path": "../escaped.log"},
    {"LOGGING.file_path": "."},
])
def test_invalid_settings_leave_logger_untouched(isolated_logging, settings):
    logger, root = isolated_logging
    original_level = logger.level
    with pytest.raises(ValueError):
        BackendLogging(settings).start()
    assert logger.handlers == []
    assert logger.level == original_level
    assert list(root.iterdir()) == []


def test_absolute_destination_and_repeated_lifecycle(isolated_logging):
    logger, root = isolated_logging
    service = BackendLogging({"LOGGING.file_path": str(root / "absolute.log")})
    assert service.stop() is True
    for _ in range(2):
        assert service.start() is service
        assert service.start() is service
        assert len(logger.handlers) == 1
        with pytest.raises(RuntimeError, match="already"):
            BackendLogging({}).start()
        logger.warning("one record")
        assert service.stop() is True
        assert service.stop() is True
        assert logger.handlers == []
    assert service.file_path.read_text().count("one record") == 2


@pytest.mark.parametrize("options", [
    {"queue_size": 0}, {"queue_size": True},
    {"shutdown_timeout_s": 0}, {"shutdown_timeout_s": float("nan")},
    {"shutdown_timeout_s": float("inf")}, {"shutdown_timeout_s": True},
])
def test_queue_and_shutdown_bounds_are_required(isolated_logging, options):
    with pytest.raises(ValueError):
        BackendLogging({}, **options)


def test_file_open_failure_leaves_logger_and_ownership_unchanged(isolated_logging):
    logger, root = isolated_logging
    (root / "blocked").write_text("not a directory")
    original_level = logger.level
    with pytest.raises(OSError):
        BackendLogging({"LOGGING.file_path": "blocked/app.log"}).start()
    assert logger.handlers == []
    assert logger.level == original_level
    assert BackendLogging._owner is None


def test_slow_format_queue_saturation_and_bounded_shutdown(isolated_logging, capsys):
    logger, _ = isolated_logging
    formatting, release, produced = Event(), Event(), Event()
    formatted_on = []

    class SlowMessage:
        def __str__(self):
            formatted_on.append(current_thread())
            formatting.set()
            assert release.wait(5)
            return "slow record"

    service = BackendLogging({}, queue_size=1, shutdown_timeout_s=0.01).start()
    producer = Thread(target=lambda: (logger.warning(SlowMessage()), produced.set()))
    try:
        producer.start()
        assert produced.wait(1), "Producer performed slow formatting"
        assert formatting.wait(1)
        logger.warning("queued record")
        logger.warning("dropped record")
        assert service.dropped_records == 1
        started = time.monotonic()
        assert service.stop() is False
        assert time.monotonic() - started < 0.5
        assert service.is_running
        assert logger.handlers == []
        with pytest.raises(RuntimeError, match="already"):
            BackendLogging({}).start()
        assert capsys.readouterr().err == ""
    finally:
        release.set()
        producer.join(1)
        service._thread.join(2)
        service.stop()
    assert formatted_on and producer not in formatted_on
    assert not service.is_running
    assert "queued record" in service.file_path.read_text()
    assert "dropped record" not in service.file_path.read_text()


def test_slow_write_does_not_block_producer_or_close_in_use_stream(isolated_logging, monkeypatch):
    logger, _ = isolated_logging
    writing, release, closed = Event(), Event(), Event()
    original_emit = backend_logging.RotatingFileHandler.emit
    original_close = backend_logging.RotatingFileHandler.close

    def slow_emit(handler, record):
        writing.set()
        assert release.wait(5)
        original_emit(handler, record)

    def tracked_close(handler):
        closed.set()
        original_close(handler)

    monkeypatch.setattr(backend_logging.RotatingFileHandler, "emit", slow_emit)
    monkeypatch.setattr(backend_logging.RotatingFileHandler, "close", tracked_close)
    service = BackendLogging({}, shutdown_timeout_s=0.01).start()
    try:
        logger.warning("slow write")
        assert writing.wait(1)
        assert service.stop() is False
        assert not closed.is_set()
    finally:
        release.set()
        service._thread.join(2)
        service.stop()
    assert closed.is_set()
    assert "slow write" in service.file_path.read_text()


def test_global_logging_shutdown_does_not_wait_for_the_writer(isolated_logging, monkeypatch):
    logger, _ = isolated_logging
    writing, release, shutdown_done = Event(), Event(), Event()
    writers = []
    original_emit = backend_logging.RotatingFileHandler.emit

    def slow_emit(handler, record):
        writers.append(handler)
        writing.set()
        assert release.wait(5)
        original_emit(handler, record)

    monkeypatch.setattr(backend_logging.RotatingFileHandler, "emit", slow_emit)
    service = BackendLogging({}).start()
    shutdown_thread = None
    try:
        logger.warning("in progress")
        assert writing.wait(1)
        # Restrict shutdown to this sink so unrelated test runner handlers survive.
        shutdown_thread = Thread(target=lambda: (
            logging.shutdown([weakref.ref(writers[0])]), shutdown_done.set(),
        ))
        shutdown_thread.start()
        assert shutdown_done.wait(1), "Global shutdown waited for blocked file I/O"
        logger.warning("still queued")
    finally:
        release.set()
        if shutdown_thread is not None:
            shutdown_thread.join(2)
        assert service.stop() is True
    assert "in progress" in service.file_path.read_text()
    assert "still queued" in service.file_path.read_text()


def test_write_failure_is_counted_without_stderr_or_recursion(isolated_logging, monkeypatch, capsys):
    logger, _ = isolated_logging
    original_format = backend_logging.RotatingFileHandler.format

    def fail_format(handler, record):
        if record.msg == "bad record":
            raise OSError("disk failed")
        return original_format(handler, record)

    monkeypatch.setattr(backend_logging.RotatingFileHandler, "format", fail_format)
    with BackendLogging({}) as service:
        logger.warning("bad record")
        logger.warning("next record")
    assert service.write_errors == 1
    assert "next record" in service.file_path.read_text()
    assert capsys.readouterr().err == ""


def test_rotation_is_bounded(isolated_logging):
    logger, _ = isolated_logging
    with BackendLogging({}) as service:
        for index in range(5):
            logger.warning("%s %s", index, "x" * (5 * 1024 * 1024))
    assert service.write_errors == 0
    assert service.file_path.exists()
    assert all(Path(f"{service.file_path}.{index}").exists() for index in range(1, 4))
    assert not Path(f"{service.file_path}.4").exists()


def test_thread_start_failure_restores_logger_and_closes_file(isolated_logging, monkeypatch):
    logger, _ = isolated_logging
    original_level = logger.level
    closed = []
    original_close = backend_logging.RotatingFileHandler.close

    def fail_start(thread):
        raise RuntimeError("cannot start writer")

    def track_close(handler):
        closed.append(handler)
        original_close(handler)

    monkeypatch.setattr(backend_logging.Thread, "start", fail_start)
    monkeypatch.setattr(backend_logging.RotatingFileHandler, "close", track_close)
    with pytest.raises(RuntimeError, match="cannot start writer"):
        BackendLogging({}).start()
    assert closed
    assert logger.handlers == []
    assert logger.level == original_level
    assert BackendLogging._owner is None
