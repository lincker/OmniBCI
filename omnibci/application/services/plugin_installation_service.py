"""Install immutable plugin versions without enabling or running them."""
from contextlib import nullcontext
from pathlib import Path

from omnibci.application.plugin_packages import PluginPackageStore


class PluginInstallationService:
    def __init__(self, store: PluginPackageStore, *, admission_guard=None):
        self.store = store
        self.admission_guard = admission_guard or nullcontext

    @property
    def max_archive_bytes(self) -> int:
        return self.store.max_archive_bytes

    def install(self, archive: Path):
        with self.admission_guard():
            return self.store.install_archive(archive)

    def list(self):
        return self.store.list_packages()
