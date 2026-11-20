"""Install immutable plugin versions without enabling or running them."""
from contextlib import nullcontext
from pathlib import Path
from typing import Callable, Mapping

from omnibci.application.plugin_packages import PluginPackageStore


class PluginInstallationService:
    def __init__(self, store: PluginPackageStore, *, admission_guard=None):
        self.store = store
        self.admission_guard = admission_guard or nullcontext

    @property
    def max_archive_bytes(self) -> int:
        return self.store.max_archive_bytes

    def install(self, archive: Path, *, revalidate: Callable[[], None] | None = None,
                expected_sha256: str | None = None, expected_package_id: str | None = None,
                expected_version: str | None = None, expected_domain: str | None = None,
                expected_contributions: Mapping[str, str] | None = None):
        with self.admission_guard():
            if revalidate is not None:
                revalidate()
            return self.store.install_archive(
                archive, expected_sha256=expected_sha256, expected_package_id=expected_package_id,
                expected_version=expected_version, expected_domain=expected_domain,
                expected_contributions=expected_contributions,
            )

    def list(self):
        return self.store.list_packages()
