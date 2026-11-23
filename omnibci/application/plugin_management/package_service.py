"""Explicit package installation and lifecycle protected by configuration and runtime references."""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping

from omnibci.application.plugin_management.packages import (
    MarketVerification,
    PackageReference,
    PluginPackageConflictError,
    PluginPackageError,
    PluginPackageStore,
)


class PluginPackageService:
    def __init__(
        self,
        store: PluginPackageStore,
        *,
        configuration_references: Callable[[], Iterable[PackageReference]] | None = None,
        runtime_references: Callable[[], Iterable[PackageReference]] | None = None,
        admission_guard=None,
    ) -> None:
        self.store = store
        self.configuration_references = configuration_references or (lambda: ())
        self.runtime_references = runtime_references or (lambda: ())
        self.admission_guard = admission_guard or nullcontext

    @property
    def max_archive_bytes(self) -> int:
        return self.store.max_archive_bytes

    def install(
        self,
        archive: Path,
        *,
        revalidate: Callable[[], MarketVerification] | None = None,
        expected_sha256: str | None = None,
        expected_package_id: str | None = None,
        expected_version: str | None = None,
        expected_domain: str | None = None,
        expected_contributions: Mapping[str, str] | None = None,
    ):
        with self.admission_guard():
            return self.store.install_archive(
                archive,
                revalidate=revalidate,
                expected_sha256=expected_sha256,
                expected_package_id=expected_package_id,
                expected_version=expected_version,
                expected_domain=expected_domain,
                expected_contributions=expected_contributions,
            )

    def references(self) -> tuple[PackageReference, ...]:
        references = list(self.configuration_references())
        references.extend(self.runtime_references())
        if any(not isinstance(item, PackageReference) for item in references):
            raise PluginPackageError("Package references must be explicit PackageReference values")
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _matching(references, package_id: str, version: str):
        return tuple(
            item
            for item in references
            if item.selection.package_id == package_id and item.selection.version == version
        )

    def list(self):
        references = self.references()
        return [
            replace(item, references=self._matching(references, item.package_id, item.version))
            for item in self.store.list_packages()
        ]

    def _require_unused(self, package_id: str, version: str) -> None:
        references = self._matching(self.references(), package_id, version)
        if references:
            descriptions = ", ".join(f"{item.scope}:{item.identifier}" for item in references)
            raise PluginPackageConflictError(f"Package version is still referenced by {descriptions}")

    def set_enabled(self, package_id: str, version: str, enabled: bool):
        with self.admission_guard():
            if not enabled:
                self._require_unused(package_id, version)
            return self.store.set_enabled(package_id, version, enabled)

    def remove(self, package_id: str, version: str) -> None:
        with self.admission_guard():
            self._require_unused(package_id, version)
            self.store.remove(package_id, version)
