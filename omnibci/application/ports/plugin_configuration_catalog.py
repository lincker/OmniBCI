"""Static plugin metadata used by configuration use cases."""
from dataclasses import dataclass, field
from typing import Protocol

from omnibci.application.plugin_profiles import PluginSelection


@dataclass(frozen=True)
class PluginConfiguration:
    selection: PluginSelection
    kind: str
    plugin_type: str
    label: str
    description: str | None
    schema: dict | None
    configuration_version: int | None
    available: bool
    controls: dict = field(default_factory=dict)


class PluginConfigurationCatalog(Protocol):
    def list(self, kind: str) -> list[PluginConfiguration]: ...
    def get(self, kind: str, selection: PluginSelection) -> PluginConfiguration: ...
    def legacy(self, kind: str, alias: str) -> PluginConfiguration: ...
