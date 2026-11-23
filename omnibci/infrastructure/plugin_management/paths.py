"""Portable, confined paths for managed package versions."""
from pathlib import Path

from omnibci.application.plugin_management.identity import is_valid_package_id, is_valid_version
from omnibci.application.plugin_management.packages import PluginPackageError, PluginPackageStorageError


def managed_package_path(root: Path, package_id: str, version: str) -> Path:
    if not is_valid_package_id(package_id) or not is_valid_version(version):
        raise PluginPackageError("Invalid exact package identity or version")
    path = root / package_id / version
    for item in (root, path.parent, path):
        if item.is_symlink():
            raise PluginPackageStorageError("Managed package paths must not contain symlinks")
    # Windows and common macOS volumes resolve differently cased names to the
    # same file. Keep lifecycle identities exact before checking registry/refs.
    try:
        for parent, name in ((root, package_id), (path.parent, version)):
            if parent.is_dir() and any(
                child.name.casefold() == name.casefold() and child.name != name
                for child in parent.iterdir()
            ):
                raise PluginPackageError("Package identity must use its exact installed spelling")
    except OSError as exc:
        raise PluginPackageStorageError(f"Cannot validate managed package path: {exc}") from exc
    return path
