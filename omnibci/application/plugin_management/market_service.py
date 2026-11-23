"""Download before acquiring exclusive runtime mutation admission."""
from omnibci.application.plugin_management.market import PluginMarketRepository
from omnibci.application.plugin_management.package_service import PluginPackageService


class PluginMarketService:
    def __init__(self, repository: PluginMarketRepository, installation: PluginPackageService):
        self.repository = repository
        self.installation = installation

    def list(self):
        return self.repository.list_packages()

    def install(self, package_id: str, version: str):
        prepared = self.repository.prepare_install(package_id, version)
        selected = prepared.package
        return self.installation.install(
            prepared.archive,
            revalidate=lambda: self.repository.verify_prepared(prepared),
            expected_sha256=selected.sha256,
            expected_package_id=selected.package_id,
            expected_version=selected.version,
            expected_domain=selected.domain,
            expected_contributions={
                item.selection.contribution_id: item.kind for item in selected.contributions
            },
        )
