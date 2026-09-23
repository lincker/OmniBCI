"""Atomic enablement state, kept separate from immutable installed package code.

Catalog discovery uses only the read-only helper at the end of this module.
This module never imports the executable loader, installer, or plugin catalog.
"""
from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from omnibci.application.plugin_packages import PluginPackageError, PluginPackageStorageError
from omnibci.infrastructure.plugins.package_paths import managed_package_path


class InstalledPackageRegistry:
    def __init__(self, plugin_dir: Path):
        self.root = Path(plugin_dir).resolve()
        self.state_dir = self.root / "state"
        self.path = self.state_dir / "enabled.json"
        self._thread_lock = threading.RLock()

    def _safe_state(self) -> None:
        if self.state_dir.is_symlink() or self.path.is_symlink():
            raise PluginPackageStorageError("Plugin enablement state must not contain symlinks")

    def read(self) -> set[tuple[str, str]]:
        self._safe_state()
        if not self.path.exists():
            return set()
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError("enablement state is too large")
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.keys() != {"schema_version", "enabled"} or type(document["schema_version"]) is not int or document["schema_version"] != 1:
                raise ValueError("unsupported enablement state")
            if not isinstance(document["enabled"], list):
                raise ValueError("enabled must be an array")
            enabled = set()
            for item in document["enabled"]:
                if not isinstance(item, dict) or item.keys() != {"package_id", "version"}:
                    raise ValueError("invalid enabled package identity")
                managed_package_path(self.root / "packages", item["package_id"], item["version"])
                pair = (item["package_id"], item["version"])
                if pair in enabled:
                    raise ValueError("duplicate enabled package")
                enabled.add(pair)
            return enabled
        except (OSError, ValueError, TypeError, PluginPackageError) as exc:
            raise PluginPackageStorageError(f"Cannot read plugin enablement state: {exc}") from exc

    @contextmanager
    def access(self):
        """Serialize read/modify/write across API threads and application processes."""
        try:
            with self._locked():
                yield
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot access plugin enablement state: {exc}") from exc

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            self._safe_state()
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self.state_dir / ".enabled.lock"
            if lock_path.is_symlink():
                raise PluginPackageStorageError("Enablement lock must not be a symlink")
            with lock_path.open("a+b") as stream:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"\0")
                        stream.flush()

                    def acquire():
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

                    def release():
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    def acquire():
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                    def release():
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                deadline = time.monotonic() + 5.0
                while True:
                    try:
                        acquire()
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                            raise PluginPackageStorageError(str(exc)) from exc
                        if time.monotonic() >= deadline:
                            raise PluginPackageStorageError("Timed out waiting for package lifecycle lock") from exc
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    release()

    def write(self, enabled: set[tuple[str, str]]) -> None:
        """Write under access(); readers always observe a complete document."""
        self._safe_state()
        document = {"schema_version": 1, "enabled": [
            {"package_id": package_id, "version": version} for package_id, version in sorted(enabled)
        ]}
        serialized = json.dumps(document, sort_keys=True)
        if len(serialized.encode("utf-8")) > 1024 * 1024:
            raise PluginPackageStorageError("Plugin enablement state exceeds the size limit")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.state_dir,
                                             prefix=".enabled-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot write plugin enablement state: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def enabled_package_directories(plugin_dir: Path) -> list[Path]:
    registry = InstalledPackageRegistry(plugin_dir)
    paths = []
    for package_id, version in sorted(registry.read()):
        path = managed_package_path(registry.root / "packages", package_id, version)
        if not path.is_dir():
            raise PluginPackageStorageError(f"Enabled package is missing: {package_id}@{version}")
        paths.append(path)
    return paths
