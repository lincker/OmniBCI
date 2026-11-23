"""Shared filesystem locking, atomic writes, and strict JSON parsing."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import tempfile
import time


@contextmanager
def file_lock(
    path: Path,
    *,
    timeout_s: float | None = 5.0,
    timeout_message: str = "Timed out waiting for the file lock",
):
    """Serialize cooperating writers across processes and platforms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError("Lock path must not contain symlinks")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()

            def acquire(*, blocking: bool) -> None:
                stream.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(stream.fileno(), mode, 1)

            def release() -> None:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire(*, blocking: bool) -> None:
                flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), flags)

            def release() -> None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        if timeout_s is None:
            acquire(blocking=True)
        else:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    acquire(blocking=False)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(timeout_message) from exc
                    time.sleep(0.01)
        try:
            yield
        finally:
            release()


def atomic_write(path: Path, content: str, *, prefix: str | None = None) -> None:
    """Replace *path* atomically using fsync and os.replace."""
    temporary = None
    temp_prefix = prefix if prefix is not None else f".{path.name}."
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=path.parent,
            prefix=temp_prefix, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def strict_json_loads(
    raw: str | bytes,
    *,
    error_cls: type[Exception] = ValueError,
    reject_non_finite: bool = False,
):
    """Parse JSON while rejecting duplicate object keys and optional NaN/Inf."""
    def unique_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise error_cls(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str):
        raise error_cls(f"Non-finite JSON value is forbidden: {value}")

    kwargs = {"object_pairs_hook": unique_object}
    if reject_non_finite:
        kwargs["parse_constant"] = reject_constant
    try:
        return json.loads(raw, **kwargs)
    except error_cls:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise error_cls(str(exc)) from exc
