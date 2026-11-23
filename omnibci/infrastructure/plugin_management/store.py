"""Managed package inventory, atomic enablement, and removal."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
from typing import Callable, Mapping
from uuid import uuid4

from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.plugin_management.packages import (
    MarketVerification,
    PackageContribution,
    PackageInstallation,
    PackageVersion,
    PluginPackageConflictError,
    PluginPackageError,
    PluginPackageNotFoundError,
    PluginPackageStorageError,
)
from omnibci.infrastructure.plugin_management.catalog import (
    CatalogConfigurationAdapter,
    PluginCatalog,
    PluginRoot,
    adapt_descriptor,
    user_plugins_dir,
)
from omnibci.infrastructure.plugin_management.installer import PackageInstallError, PluginPackageInstaller
from omnibci.infrastructure.plugin_management.manifest import PluginResolutionError
from omnibci.infrastructure.plugin_management.paths import managed_package_path
from omnibci.infrastructure.plugin_management.registry import InstalledPackageRegistry


class ManagedPluginPackageStore:
    """Install, list, verify, enable, disable, and remove managed package versions."""

    def __init__(self, plugin_dir: Path | None = None, *, installer: PluginPackageInstaller | None = None):
        directory = plugin_dir if plugin_dir is not None else user_plugins_dir()
        if directory is None:
            raise PluginPackageStorageError("User plugin directory is unavailable")
        self.plugin_dir = Path(directory).resolve()
        self.packages_dir = self.plugin_dir / "packages"
        self.installer = installer or PluginPackageInstaller(self.packages_dir)
        if self.installer.root != self.packages_dir:
            raise ValueError("Installer must use the managed packages directory")
        self.catalog = PluginCatalog(plugin_dir=self.plugin_dir, host_version=self.installer.host_version)
        self.registry = InstalledPackageRegistry(self.plugin_dir)
        self.max_archive_bytes = self.installer.limits.max_archive_bytes

    def _path(self, package_id: str, version: str) -> Path:
        path = managed_package_path(self.packages_dir, package_id, version)
        if not path.is_dir():
            raise PluginPackageNotFoundError(f"Installed package not found: {package_id}@{version}")
        return path

    def _descriptors(self, package_id: str, version: str):
        location = self._path(package_id, version)
        descriptors = self.installer.validate_package(location)
        if any(item.identifier != package_id or item.version != version for item in descriptors):
            raise PluginPackageError("Installed manifest identity differs from its directory")
        return descriptors

    def _read_package(self, package_id: str, version: str, *, enabled: bool) -> PackageVersion:
        location = self._path(package_id, version)
        try:
            receipt = self.installer.read_receipt(location, package_id, version)
            descriptors = self._descriptors(package_id, version)
            first = descriptors[0]
            definitions = [adapt_descriptor(item, self.installer.platform_name) for item in descriptors]
            return PackageVersion(
                package_id,
                version,
                first.display_name,
                first.description,
                receipt["sha256"],
                tuple(
                    PackageContribution(item.selection, item.kind, item.label, item.available)
                    for item in definitions
                ),
                domain=first.package.domain,
                enabled=enabled,
                available=any(item.available for item in definitions),
                origin=receipt["origin"],
                verified_repository=(
                    receipt["market_verification"]["repository_id"]
                    if receipt["market_verification"] is not None
                    else None
                ),
            )
        except (OSError, ValueError, KeyError, PluginResolutionError, PackageInstallError, PluginPackageError) as exc:
            return PackageVersion(
                package_id, version, package_id, None, None, (),
                domain=None, enabled=enabled, available=False, error=str(exc),
            )

    def install_archive(
        self,
        archive: Path,
        *,
        revalidate: Callable[[], MarketVerification] | None = None,
        expected_sha256: str | None = None,
        expected_package_id: str | None = None,
        expected_version: str | None = None,
        expected_domain: str | None = None,
        expected_contributions: Mapping[str, str] | None = None,
    ) -> PackageInstallation:
        try:
            installed = self.installer.install(
                archive,
                revalidate=revalidate,
                expected_sha256=expected_sha256,
                expected_package_id=expected_package_id,
                expected_version=expected_version,
                expected_domain=expected_domain,
                expected_contributions=expected_contributions,
            )
            return PackageInstallation(
                self.get_package(installed.package_id, installed.version),
                installed.already_installed,
            )
        except PackageInstallError as exc:
            raise PluginPackageError(str(exc)) from exc
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot access package storage: {exc}") from exc

    def get_package(self, package_id: str, version: str) -> PackageVersion:
        enabled = (package_id, version) in self.registry.read()
        return self._read_package(package_id, version, enabled=enabled)

    def verify_integrity(self, package_id: str, version: str) -> None:
        location = self._path(package_id, version)
        try:
            self.installer.verify_existing(location, self.installer.read_receipt(location, package_id, version))
        except (OSError, ValueError, PackageInstallError) as exc:
            raise PluginPackageError(str(exc)) from exc

    def list_packages(self) -> list[PackageVersion]:
        if self.packages_dir.is_symlink():
            raise PluginPackageStorageError("Managed packages directory must not be a symlink")
        enabled = self.registry.read()
        packages = []
        try:
            for family in sorted(self.packages_dir.iterdir()) if self.packages_dir.exists() else ():
                if family.name.startswith("__") or not family.is_dir():
                    continue
                if family.is_symlink():
                    raise PluginPackageStorageError("Managed package directory must not be a symlink")
                for version in sorted(family.iterdir()):
                    if version.is_dir():
                        packages.append(
                            self._read_package(
                                family.name,
                                version.name,
                                enabled=(family.name, version.name) in enabled,
                            )
                        )
            return packages
        except OSError as exc:
            raise PluginPackageStorageError(f"Cannot read installed packages: {exc}") from exc

    def configuration(self, kind: str, selection: PluginSelection):
        location = self._path(selection.package_id, selection.version)
        catalog = PluginCatalog({kind: lambda: [PluginRoot(location, "user", 0)]}, host_version=self.installer.host_version)
        return CatalogConfigurationAdapter(catalog, platform_name=self.installer.platform_name).get(kind, selection)

    def set_enabled(self, package_id: str, version: str, enabled: bool) -> PackageVersion:
        if type(enabled) is not bool:
            raise PluginPackageError("enabled must be a boolean")
        with self.registry.access():
            current = self.registry.read()
            pair = (package_id, version)
            was_enabled = pair in current
            package = self._read_package(package_id, version, enabled=was_enabled)
            if enabled:
                if package.error is not None or not package.available:
                    raise PluginPackageError(package.error or "Package is not compatible with this host")
                self.verify_integrity(package_id, version)
                expected_location = self._path(package_id, version)
                for existing in self.catalog.scan_all():
                    if (
                        existing.identifier == package_id
                        and existing.version == version
                        and existing.location != expected_location
                    ):
                        raise PluginPackageConflictError(
                            "Another catalog location already provides this exact package version"
                        )
                current.add(pair)
            else:
                current.discard(pair)
            if enabled != was_enabled:
                self.registry.write(current)
            return replace(package, enabled=enabled)

    def remove(self, package_id: str, version: str) -> None:
        with self.registry.access():
            if (package_id, version) in self.registry.read():
                raise PluginPackageConflictError("Disable this package version before uninstalling it")
            source = self._path(package_id, version)
            removed = self.packages_dir / f"__remove-{uuid4()}"
            try:
                source.rename(removed)
            except OSError as exc:
                raise PluginPackageStorageError(f"Cannot remove package version: {exc}") from exc
            try:
                shutil.rmtree(removed)
            except OSError as exc:
                raise PluginPackageStorageError(
                    f"Package is no longer installed; cleanup remains at {removed.name}: {exc}"
                ) from exc
