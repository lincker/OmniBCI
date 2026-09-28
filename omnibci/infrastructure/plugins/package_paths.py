"""Portable, confined paths for managed package versions."""
import re
from pathlib import Path

from omnibci.application.plugin_packages import PluginPackageError, PluginPackageStorageError


def managed_package_path(root: Path, package_id: str, version: str) -> Path:
    if not isinstance(package_id, str) or len(package_id) > 255 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*\.[A-Za-z0-9][A-Za-z0-9_-]*", package_id,
    ) or not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,127}", version) or version.endswith("."):
        raise PluginPackageError("Invalid exact package identity or version")
    path = root / package_id / version
    for item in (root, path.parent, path):
        if item.is_symlink():
            raise PluginPackageStorageError("Managed package paths must not contain symlinks")
    # Windows and common macOS volumes resolve differently cased names to the
    # same file. Keep lifecycle identities exact before checking registry/refs.
    try:
        for parent, name in ((root, package_id), (path.parent, version)):
            if parent.is_dir() and any(child.name.casefold() == name.casefold() and child.name != name
                                       for child in parent.iterdir()):
                raise PluginPackageError("Package identity must use its exact installed spelling")
    except OSError as exc:
        raise PluginPackageStorageError(f"Cannot validate managed package path: {exc}") from exc
    return path
