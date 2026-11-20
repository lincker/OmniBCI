"""Adapt read-only package descriptors to application configuration metadata."""
import sys
from copy import deepcopy

from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration
from omnibci.application.plugin_identity import PluginSelection
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.infrastructure.plugins.plugin_catalog import (
    PluginCatalog,
)
from omnibci.infrastructure.plugins.manifest import PluginResolutionError


class CatalogConfigurationAdapter:
    def __init__(self, catalog: PluginCatalog | None = None, *, platform_name: str | None = None):
        self.catalog = catalog or PluginCatalog()
        self.platform_name = platform_name if platform_name is not None else sys.platform

    @staticmethod
    def _adapt(descriptor, platform_name: str) -> PluginConfiguration:
        return PluginConfiguration(
            selection=descriptor.selection, kind=descriptor.category, domain=descriptor.package.domain,
            plugin_type=descriptor.plugin_type, label=descriptor.display_name,
            description=descriptor.description,
            schema=deepcopy(descriptor.configuration_schema),
            configuration_version=descriptor.configuration_version,
            available=descriptor.supports_platform(platform_name),
            controls=deepcopy(dict(descriptor.controls)),
            channel_requirements=descriptor.channel_requirements,
        )

    def list(self, kind: str) -> list[PluginConfiguration]:
        return [self._adapt(descriptor, self.platform_name) for descriptor in self.catalog.list_contributions(kind)]

    def get(self, kind: str, selection: PluginSelection) -> PluginConfiguration:
        try:
            return self._adapt(self.catalog.resolve_selection(kind, selection), self.platform_name)
        except PluginResolutionError as exc:
            raise PluginSchemaError(str(exc)) from exc
