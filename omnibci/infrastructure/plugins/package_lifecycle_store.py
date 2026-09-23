"""Enablement and removal of managed package versions; configuration stays pinned."""
import shutil
from dataclasses import replace
from uuid import uuid4

from omnibci.application.plugin_packages import PluginPackageConflictError, PluginPackageError, PluginPackageStorageError
from omnibci.application.plugin_profiles import PLUGIN_KINDS
from omnibci.infrastructure.plugins.installed_registry import InstalledPackageRegistry
from omnibci.infrastructure.plugins.package_store import DirectoryPluginPackageStore
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog


class ManagedPluginPackageStore(DirectoryPluginPackageStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.registry = InstalledPackageRegistry(self.plugin_dir)

    def get_package(self, package_id, version):
        package = super().get_package(package_id, version)
        return replace(package, enabled=(package_id, version) in self.registry.read())

    def set_enabled(self, package_id: str, version: str, enabled: bool):
        if type(enabled) is not bool:
            raise PluginPackageError("enabled must be a boolean")
        with self.registry.access():
            package = self.get_package(package_id, version)
            current = self.registry.read()
            pair = (package_id, version)
            if enabled:
                if package.error is not None or not package.available:
                    raise PluginPackageError(package.error or "Package is not compatible with this host")
                self.verify_integrity(package_id, version)
                catalog = PluginCatalog()
                for kind in sorted(PLUGIN_KINDS):
                    for existing in catalog.scan(kind):
                        if existing.identifier == package_id and existing.version == version and existing.location != self._path(package_id, version):
                            raise PluginPackageConflictError("Another catalog location already provides this exact package version")
                current.add(pair)
            else:
                current.discard(pair)
            if current != self.registry.read():
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
