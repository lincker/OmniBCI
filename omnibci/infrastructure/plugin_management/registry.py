"""Atomic enablement state, kept separate from immutable installed package code.

Catalog discovery uses only the read-only helper at the end of this module.
This module never imports the executable loader, installer, or plugin catalog.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import threading

from omnibci.application.plugin_management.packages import PluginPackageError, PluginPackageStorageError
from omnibci.infrastructure.plugin_management.paths import managed_package_path
from omnibci.infrastructure.storage import atomic_write, file_lock


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
            if (
                not isinstance(document, dict)
                or document.keys() != {"schema_version", "enabled"}
                or type(document["schema_version"]) is not int
                or document["schema_version"] != 1
            ):
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
            with self._thread_lock:
                self._safe_state()
                self.state_dir.mkdir(parents=True, exist_ok=True)
                with file_lock(
                    self.state_dir / ".enabled.lock",
                    timeout_s=5.0,
                    timeout_message="Timed out waiting for package lifecycle lock",
                ):
                    yield
        except (OSError, TimeoutError) as exc:
            raise PluginPackageStorageError(f"Cannot access plugin enablement state: {exc}") from exc

    def write(self, enabled: set[tuple[str, str]]) -> None:
        """Write under access(); readers always observe a complete document."""
        self._safe_state()
        document = {
            "schema_version": 1,
            "enabled": [
                {"package_id": package_id, "version": version}
                for package_id, version in sorted(enabled)
            ],
        }
        serialized = json.dumps(document, sort_keys=True)
        if len(serialized.encode("utf-8")) > 1024 * 1024:
            raise PluginPackageStorageError("Plugin enablement state exceeds the size limit")
        try:
            atomic_write(self.path, serialized, prefix=".enabled-")
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot write plugin enablement state: {exc}") from exc


def enabled_package_directories(plugin_dir: Path) -> list[Path]:
    registry = InstalledPackageRegistry(plugin_dir)
    paths = []
    for package_id, version in sorted(registry.read()):
        path = managed_package_path(registry.root / "packages", package_id, version)
        if not path.is_dir():
            raise PluginPackageStorageError(f"Enabled package is missing: {package_id}@{version}")
        paths.append(path)
    return paths
