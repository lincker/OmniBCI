"""Installed package inventory; installation does not add any catalog roots."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from omnibci.application.plugin_packages import (
    MarketVerification, PackageContribution, PackageInstallation, PackageVersion, PluginPackageError,
    PluginPackageNotFoundError, PluginPackageStorageError,
)
from omnibci.application.plugin_identity import PluginSelection
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.package_installer import PackageInstallError, PluginPackageInstaller
from omnibci.infrastructure.plugins.package_paths import managed_package_path
from omnibci.infrastructure.plugins.plugin_catalog import (
    PluginCatalog, PluginRoot, user_plugins_dir,
)
from omnibci.infrastructure.plugins.manifest import PluginResolutionError


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
        self.catalog = self.installer.catalog
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

    def install_archive(self, archive: Path, *, revalidate: Callable[[], MarketVerification] | None = None,
                        expected_sha256: str | None = None,
                        expected_package_id: str | None = None, expected_version: str | None = None,
                        expected_domain: str | None = None,
                        expected_contributions: Mapping[str, str] | None = None) -> PackageInstallation:
        try:
            installed = self.installer.install(
                archive, revalidate=revalidate, expected_sha256=expected_sha256, expected_package_id=expected_package_id,
                expected_version=expected_version, expected_domain=expected_domain,
                expected_contributions=expected_contributions,
            )
            return PackageInstallation(self.get_package(installed.package_id, installed.version), installed.already_installed)
        except PackageInstallError as exc:
            raise PluginPackageError(str(exc)) from exc
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot access package storage: {exc}") from exc

    def get_package(self, package_id: str, version: str) -> PackageVersion:
        location = self._path(package_id, version)
        try:
            receipt = self.installer.read_receipt(location, package_id, version)
            descriptors = self._descriptors(package_id, version)
            first = descriptors[0]
            definitions = [CatalogConfigurationAdapter._adapt(item, self.installer.platform_name) for item in descriptors]
            return PackageVersion(
                package_id, version, first.display_name, first.description, receipt["sha256"],
                tuple(PackageContribution(item.selection, item.kind, item.label, item.available) for item in definitions),
                domain=first.package.domain, available=any(item.available for item in definitions),
                origin=receipt["origin"],
                verified_repository=(receipt["market_verification"]["repository_id"]
                                     if receipt["market_verification"] is not None else None),
            )
        except (OSError, ValueError, KeyError, PluginResolutionError, PackageInstallError, PluginPackageError) as exc:
            return PackageVersion(package_id, version, package_id, None, None, (), domain=None, available=False, error=str(exc))

    def verify_integrity(self, package_id: str, version: str) -> None:
        location = self._path(package_id, version)
        try:
            self.installer._verify_existing(location, self.installer.read_receipt(location, package_id, version))
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
        location = self._path(selection.package_id, selection.version)
        catalog = PluginCatalog({kind: lambda: [PluginRoot(location, "user", 0)]}, host_version=self.installer.host_version)
        return CatalogConfigurationAdapter(catalog, platform_name=self.installer.platform_name).get(kind, selection)
