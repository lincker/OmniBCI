"""Process-owned leases for local stores shared by backend instances."""

import os
from pathlib import Path


class StorageInUseError(RuntimeError):
    """Another live owner holds the requested storage lease."""


class StorageLease:
    """Hold a nonblocking OS lock until close; crashes release it automatically."""

    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if path.stat().st_size == 0:
                    self._stream.write(b"\0")
                    self._stream.flush()
                self._stream.seek(0)
                msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._stream.close()
            self._stream = None
            raise StorageInUseError(f"Storage is owned by another process: {path}") from exc

    def close(self):
        if self._stream is not None:
            stream, self._stream = self._stream, None
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
