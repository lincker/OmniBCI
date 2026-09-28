"""Adapt read-only package descriptors to application configuration metadata."""
import sys
from copy import deepcopy

from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration
from omnibci.application.plugin_profiles import PluginSelection
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, PluginResolutionError


class CatalogConfigurationAdapter:
    def __init__(self, catalog: PluginCatalog | None = None):
        self.catalog = catalog or PluginCatalog()

    @staticmethod
    def _adapt(descriptor) -> PluginConfiguration:
        return PluginConfiguration(
            selection=descriptor.selection, kind=descriptor.category,
            plugin_type=descriptor.plugin_type, label=descriptor.display_name,
            description=descriptor.description,
            schema=deepcopy(descriptor.configuration_schema),
            configuration_version=descriptor.configuration_version,
            available=descriptor.supports_platform(sys.platform),
            controls=deepcopy(dict(descriptor.controls)),
        )

    def list(self, kind: str) -> list[PluginConfiguration]:
        return [self._adapt(descriptor) for descriptor in self.catalog.list_contributions(kind)]

    def get(self, kind: str, selection: PluginSelection) -> PluginConfiguration:
        try:
            return self._adapt(self.catalog.resolve_selection(kind, selection))
        except PluginResolutionError as exc:
            raise PluginSchemaError(str(exc)) from exc

    def legacy(self, kind: str, alias: str) -> PluginConfiguration:
        try:
            return self._adapt(self.catalog.resolve_legacy(kind, alias))
        except PluginResolutionError as exc:
            raise PluginSchemaError(str(exc)) from exc
