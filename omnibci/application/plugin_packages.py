"""Package-management values and storage port, independent of installation format."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from omnibci.application.plugin_profiles import PluginSelection
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration


class PluginPackageError(RuntimeError):
    """A package operation could not safely complete."""


class PluginPackageNotFoundError(PluginPackageError):
    pass


class PluginPackageConflictError(PluginPackageError):
    pass


class PluginPackageStorageError(PluginPackageError):
    pass


@dataclass(frozen=True, slots=True)
class PackageReference:
    scope: str
    identifier: str
    selection: PluginSelection


@dataclass(frozen=True, slots=True)
class PackageContribution:
    selection: PluginSelection
    kind: str
    label: str
    available: bool


@dataclass(frozen=True, slots=True)
class PackageVersion:
    package_id: str
    version: str
    label: str
    description: str | None
    sha256: str | None
    contributions: tuple[PackageContribution, ...]
    enabled: bool = False
    available: bool = True
    error: str | None = None
    references: tuple[PackageReference, ...] = ()


@dataclass(frozen=True, slots=True)
class PackageInstallation:
    package: PackageVersion
    already_installed: bool


class PluginPackageStore(Protocol):
    max_archive_bytes: int

    def install_archive(self, archive: Path) -> PackageInstallation: ...
    def list_packages(self) -> list[PackageVersion]: ...
    def get_package(self, package_id: str, version: str) -> PackageVersion: ...
    def configuration(self, kind: str, selection: PluginSelection) -> PluginConfiguration: ...


class ManagedPluginPackageStore(PluginPackageStore, Protocol):
    def set_enabled(self, package_id: str, version: str, enabled: bool) -> PackageVersion: ...
    def remove(self, package_id: str, version: str) -> None: ...


def package_references_from_settings(settings: dict | None, scope: str = "running") -> tuple[PackageReference, ...]:
    """Adapt one pinned worker/apply snapshot without inferring runtime ownership."""
    if settings is None:
        return ()
    from omnibci.application.plugin_profiles import ProfileBinding
    return tuple(
        PackageReference(scope, binding.slot, binding.selection)
        for item in settings.get("PLUGINS.bindings", [])
        for binding in (ProfileBinding.from_dict(item),)
    )
