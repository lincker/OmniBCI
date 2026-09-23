"""Download before acquiring exclusive runtime mutation admission."""
from omnibci.application.plugin_market import PluginMarketRepository


class PluginMarketService:
    def __init__(self, repository: PluginMarketRepository, *, admission_guard):
        self.repository = repository
        self.admission_guard = admission_guard

    def list(self):
        return self.repository.list_packages()

    def install(self, package_id: str, version: str):
        prepared = self.repository.prepare_install(package_id, version)
        with self.admission_guard():
            return self.repository.install_prepared(prepared)
