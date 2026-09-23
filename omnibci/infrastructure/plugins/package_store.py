"""Installed package inventory; installation does not add any catalog roots."""
from __future__ import annotations

import json
import re
from pathlib import Path

from omnibci.application.plugin_packages import (
    PackageContribution, PackageInstallation, PackageVersion, PluginPackageError,
    PluginPackageNotFoundError, PluginPackageStorageError,
)
from omnibci.application.plugin_profiles import PluginSelection
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.package_installer import PackageInstallError, PluginPackageInstaller, _portable_path
from omnibci.infrastructure.plugins.package_paths import managed_package_path
from omnibci.infrastructure.plugins.plugin_catalog import (
    PluginCatalog, PluginResolutionError, PluginRoot, user_plugins_dir,
)


class DirectoryPluginPackageStore:
    def __init__(self, plugin_dir: Path | None = None, *, installer: PluginPackageInstaller | None = None):
        directory = plugin_dir if plugin_dir is not None else user_plugins_dir()
        if directory is None:
            raise PluginPackageStorageError("User plugin directory is unavailable")
        self.plugin_dir = Path(directory).resolve()
        self.packages_dir = self.plugin_dir / "packages"
        self.installer = installer or PluginPackageInstaller(self.packages_dir)
        if self.installer.root != self.packages_dir:
            raise ValueError("Installer must use the managed packages directory")
        self.max_archive_bytes = self.installer.limits.max_archive_bytes

    def _path(self, package_id: str, version: str) -> Path:
        path = managed_package_path(self.packages_dir, package_id, version)
        if not path.is_dir():
            raise PluginPackageNotFoundError(f"Installed package not found: {package_id}@{version}")
        return path

    def _descriptors(self, package_id: str, version: str):
        location = self._path(package_id, version)
        descriptors = self.installer._validate_package(location)
        if any(item.identifier != package_id or item.version != version for item in descriptors):
            raise PluginPackageError("Installed manifest identity differs from its directory")
        return descriptors

    def install_archive(self, archive: Path) -> PackageInstallation:
        try:
            installed = self.installer.install(archive)
            return PackageInstallation(self.get_package(installed.package_id, installed.version), installed.already_installed)
        except PackageInstallError as exc:
            raise PluginPackageError(str(exc)) from exc

    def get_package(self, package_id: str, version: str) -> PackageVersion:
        location = self._path(package_id, version)
        try:
            receipt = self._receipt(location, package_id, version)
            descriptors = self._descriptors(package_id, version)
            first = descriptors[0]
            definitions = [CatalogConfigurationAdapter._adapt(item) for item in descriptors]
            return PackageVersion(
                package_id, version, first.display_name, first.description, receipt["sha256"],
                tuple(PackageContribution(item.selection, item.kind, item.label, item.available) for item in definitions),
                available=any(item.available for item in definitions),
            )
        except (OSError, ValueError, KeyError, PluginResolutionError, PackageInstallError, PluginPackageError) as exc:
            return PackageVersion(package_id, version, package_id, None, None, (), available=False, error=str(exc))

    def _receipt(self, location: Path, package_id: str, version: str) -> dict:
        path = location / ".omnibci-install.json"
        # JSON escapes can expand each permitted archive path character sixfold.
        receipt_limit = self.installer.limits.max_entries * (6 * 4096 + 128) + 4096
        if path.is_symlink() or not path.is_file() or path.stat().st_size > receipt_limit:
            raise PluginPackageError("Installation receipt must be a bounded regular file")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict)
                or receipt.keys() != {"receipt_version", "package_id", "version", "sha256", "files"}
                or type(receipt["receipt_version"]) is not int or receipt["receipt_version"] != 1
                or receipt["package_id"] != package_id or receipt["version"] != version):
            raise PluginPackageError("Installation receipt identity or format is invalid")
        files = receipt["files"]
        if not isinstance(files, dict) or not files or len(files) > self.installer.limits.max_entries:
            raise PluginPackageError("Installation receipt file list is invalid")
        for value in [receipt["sha256"], *files.values()]:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise PluginPackageError("Installation receipt digest is invalid")
        for name in files:
            _portable_path(name)
        if "manifest.toml" not in files:
            raise PluginPackageError("Installation receipt does not include the manifest")
        return receipt

    def verify_integrity(self, package_id: str, version: str) -> None:
        location = self._path(package_id, version)
        try:
            self.installer._verify_existing(location, self._receipt(location, package_id, version))
        except (OSError, ValueError, PackageInstallError) as exc:
            raise PluginPackageError(str(exc)) from exc

    def list_packages(self) -> list[PackageVersion]:
        if self.packages_dir.is_symlink():
            raise PluginPackageStorageError("Managed packages directory must not be a symlink")
        packages = []
        try:
            for family in sorted(self.packages_dir.iterdir()) if self.packages_dir.exists() else ():
                if family.name.startswith("__") or not family.is_dir():
                    continue
                if family.is_symlink():
                    raise PluginPackageStorageError("Managed package directory must not be a symlink")
                for version in sorted(family.iterdir()):
                    if version.is_dir():
                        packages.append(self.get_package(family.name, version.name))
            return packages
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot read installed packages: {exc}") from exc

    def configuration(self, kind: str, selection: PluginSelection):
        if selection.version is None:
            raise PluginPackageError("Installed packages require an exact version")
        location = self._path(selection.package_id, selection.version)
        catalog = PluginCatalog({kind: lambda: [PluginRoot(location, "user", 0)]})
        return CatalogConfigurationAdapter(catalog).get(kind, selection)
