"""Bounded, asynchronous file logging for the backend process only."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext
from copy import copy
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread, current_thread

from platformdirs import user_log_path


class _QueuedHandler(logging.Handler):
    def __init__(self, owner: BackendLogging):
        super().__init__(owner._level)
        self.owner = owner
        self.accepting = True

    def emit(self, record: logging.LogRecord) -> None:
        if not self.accepting:
            return
        try:
            # Keep formatting, including exception rendering, on the writer thread.
            self.owner._queue.put_nowait(copy(record))
        except Full:
            self.owner.dropped_records += 1


class _ConsoleFallback(logging.Handler):
    def __init__(self, owner: BackendLogging, target: logging.Handler):
        super().__init__()
        self.owner, self.target = owner, target

    def emit(self, record: logging.LogRecord) -> None:
        logger = logging.getLogger(record.name)
        while logger is not None:
            if any(handler not in (self, self.owner._handler) for handler in logger.handlers):
                return
            if not logger.propagate:
                break
            logger = logger.parent
        if record.levelno >= self.target.level:
            self.target.handle(record)


class _FileHandler(RotatingFileHandler):
    def __init__(self, owner: BackendLogging):
        self.owner = owner
        super().__init__(owner.file_path, maxBytes=5 * 1024 * 1024,
                         backupCount=3, encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
        ))

    def createLock(self) -> None:
        # Only the writer uses this sink. A handler lock would let the global
        # logging.shutdown() wait indefinitely for blocked writer I/O.
        self.lock = nullcontext()

    def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass

    def flush(self) -> None:
        if current_thread() is self.owner._thread or not self.owner.is_running:
            super().flush()

    def close(self) -> None:
        # External shutdown must not close an in-use stream. Before thread
        # startup, the management thread can still clean up a preflight failure.
        if current_thread() is self.owner._thread or not self.owner.is_running:
            super().close()

    def handleError(self, record: logging.LogRecord) -> None:
        # Logging failures must not recurse or synchronously write to stderr.
        self.owner.write_errors += 1


class BackendLogging:
    """Own one file sink while preserving existing console handlers/propagation.

    A timed-out stop detaches producers immediately; the daemon writer retains
    ownership until its outstanding I/O finishes and it closes the file.
    """

    _ownership_lock = Lock()
    _owner: BackendLogging | None = None

    def __init__(self, settings: Mapping[str, object], *, queue_size: int = 1024,
                 shutdown_timeout_s: float = 1.0):
        level = settings.get("LOGGING.level", "INFO")
        if not isinstance(level, str) or level not in {
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
        }:
            raise ValueError("LOGGING.level must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        value = settings.get("LOGGING.file_path", "omnibci.log")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("LOGGING.file_path must be a nonempty string")
        path = Path(value).expanduser()
        if not path.is_absolute():
            root = user_log_path("OmniBCI", appauthor=False).resolve()
            path = (root / path).resolve()
            if path == root or not path.is_relative_to(root):
                raise ValueError("LOGGING.file_path must stay within the user log directory")
        self.file_path = path.with_name(f"{path.stem}-backend-{os.getpid()}{path.suffix}")
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if (isinstance(shutdown_timeout_s, bool)
                or not isinstance(shutdown_timeout_s, (int, float))
                or not math.isfinite(shutdown_timeout_s) or shutdown_timeout_s <= 0):
            raise ValueError("shutdown_timeout_s must be finite and positive")
        self._level = level
        self._queue_size = queue_size
        self._shutdown_timeout_s = shutdown_timeout_s
        self._queue: Queue[logging.LogRecord] = Queue(queue_size)
        self._stopping = Event()
        self._thread: Thread | None = None
        self._handler: _QueuedHandler | None = None
        self._fallback: logging.Handler | None = None
        self._logger = logging.getLogger("omnibci")
        self._previous: tuple[int, bool] | None = None
        self.dropped_records = 0
        self.write_errors = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def read_tail(self) -> tuple[list[str], bool]:
        """Read at most 64 KiB from the current file and return its last 200 lines."""
        with self.file_path.open("rb", buffering=0) as stream:
            size = stream.seek(0, os.SEEK_END)
            offset = max(0, size - 64 * 1024)
            stream.seek(offset)
            content = stream.read(size - offset)
        truncated = offset > 0
        if truncated:
            # Reserve one byte of the read budget to recognize a line boundary.
            previous, content = content[:1], content[1:]
            if previous != b"\n":
                newline = content.find(b"\n")
                if 0 <= newline < len(content) - 1:
                    content = content[newline + 1:]
                else:
                    # A single oversized line still has a useful readable tail.
                    while content and content[0] & 0xC0 == 0x80:
                        content = content[1:]
        lines = content.decode("utf-8", errors="replace").splitlines()
        return lines[-200:], truncated or len(lines) > 200

    def start(self) -> BackendLogging:
        with self._ownership_lock:
            if BackendLogging._owner is self and self._handler is not None:
                return self
            if BackendLogging._owner is not None:
                raise RuntimeError("Backend file logging already has an active writer")
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            writer = _FileHandler(self)
            self._queue = Queue(self._queue_size)
            self._stopping = Event()
            self._handler = _QueuedHandler(self)
            self._previous = (self._logger.level, self._logger.propagate)
            if not self._logger.hasHandlers() and logging.lastResort is not None:
                # A file sink suppresses logging's implicit console fallback.
                # Preserve that existing synchronous output without replacing it.
                self._fallback = _ConsoleFallback(self, logging.lastResort)
                self._logger.addHandler(self._fallback)
            self._logger.setLevel(self._level)
            self._logger.addHandler(self._handler)
            BackendLogging._owner = self
            self._thread = Thread(target=self._write_records, args=(writer,),
                                  name="omnibci-backend-log-writer", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._detach()
                BackendLogging._owner = None
                self._thread = None
                writer.close()
                raise
        return self

    def _detach(self) -> None:
        if self._handler is None:
            return
        self._logger.removeHandler(self._handler)
        self._handler.acquire()
        try:
            self._handler.accepting = False
        finally:
            self._handler.release()
        self._handler.close()
        self._handler = None
        if self._fallback is not None:
            self._logger.removeHandler(self._fallback)
            self._fallback.close()
            self._fallback = None
        self._logger.setLevel(self._previous[0])
        self._logger.propagate = self._previous[1]

    def stop(self) -> bool:
        with self._ownership_lock:
            self._detach()
            self._stopping.set()
            thread = self._thread
        if thread is not None:
            thread.join(self._shutdown_timeout_s)
        return not self.is_running

    def _write_records(self, writer: _FileHandler) -> None:
        try:
            while not self._stopping.is_set() or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.05)
                except Empty:
                    continue
                try:
                    writer.handle(record)
                except Exception:
                    self.write_errors += 1
                finally:
                    self._queue.task_done()
        finally:
            try:
                writer.close()
            except Exception:
                self.write_errors += 1
            with self._ownership_lock:
                BackendLogging._owner = None

    def __enter__(self) -> BackendLogging:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
