"""Explicit package lifecycle protected by configuration and runtime references."""
from dataclasses import replace
from typing import Callable, Iterable

from omnibci.application.plugin_packages import (
    ManagedPluginPackageStore, PackageReference, PluginPackageConflictError, PluginPackageError,
)
from omnibci.application.services.plugin_installation_service import PluginInstallationService


class PluginPackageService(PluginInstallationService):
    def __init__(self, store: ManagedPluginPackageStore, *,
                 configuration_references: Callable[[], Iterable[PackageReference]],
                 runtime_references: Callable[[], Iterable[PackageReference]], admission_guard):
        super().__init__(store, admission_guard=admission_guard)
        self.configuration_references = configuration_references
        self.runtime_references = runtime_references

    def references(self) -> tuple[PackageReference, ...]:
        references = list(self.configuration_references())
        references.extend(self.runtime_references())
        if any(not isinstance(item, PackageReference) for item in references):
            raise PluginPackageError("Package references must be explicit PackageReference values")
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _matching(references, package_id, version):
        return tuple(item for item in references if item.selection.package_id == package_id
                     and item.selection.version == version)

    def list(self):
        references = self.references()
        return [replace(item, references=self._matching(references, item.package_id, item.version))
                for item in self.store.list_packages()]

    def _require_unused(self, package_id, version):
        references = self._matching(self.references(), package_id, version)
        if references:
            descriptions = ", ".join(f"{item.scope}:{item.identifier}" for item in references)
            raise PluginPackageConflictError(f"Package version is still referenced by {descriptions}")

    def set_enabled(self, package_id: str, version: str, enabled: bool):
        with self.admission_guard():
            if not enabled:
                self._require_unused(package_id, version)
            return self.store.set_enabled(package_id, version, enabled)

    def remove(self, package_id: str, version: str):
        with self.admission_guard():
            self._require_unused(package_id, version)
            self.store.remove(package_id, version)
