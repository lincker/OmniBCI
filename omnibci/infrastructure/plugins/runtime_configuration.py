"""Resolve only selected capabilities from a process's pinned configuration."""
from copy import deepcopy

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.sdk import PluginContext


def configured_bindings(snapshot: dict) -> tuple[ProfileBinding, ...]:
    """Require explicit selections, including an intentional empty list."""
    if "PLUGINS.bindings" not in snapshot:
        raise PluginLoadError("Runtime configuration requires pinned PLUGINS.bindings")
    raw = snapshot["PLUGINS.bindings"]
    if not isinstance(raw, list):
        raise PluginLoadError("PLUGINS.bindings must be a list")
    bindings = tuple(ProfileBinding.from_dict(value) for value in raw)
    if len({binding.slot for binding in bindings}) != len(bindings):
        raise PluginLoadError("Duplicate plugin slot")
    return bindings


def plugin_context(snapshot: dict) -> PluginContext:
    return PluginContext(
        sample_rate=snapshot.get("ACQUISITION.sample_rate"),
        channel_names=tuple(snapshot.get("ACQUISITION.channel_names", ())),
        config_revision=snapshot.get("CONFIG.revision"),
    )


def load_configured_plugin(snapshot: dict, slot: str, kind: str, expected_base,
                           *, context: PluginContext | None = None,
                           catalog: PluginCatalog | None = None, loader=PluginLoader):
    """Use exact accepted selections; never substitute aliases or versions."""
    bindings = configured_bindings(snapshot)
    binding = next((binding for binding in bindings if binding.slot == slot), None)
    if binding is None or binding.kind != kind:
        raise PluginLoadError(f"No {kind} selected for {slot}")
    catalog = catalog or PluginCatalog()
    descriptor = catalog.resolve_selection(kind, binding.selection)
    if ((descriptor.api_version == "2" or descriptor.configuration_version is not None)
            and descriptor.configuration_version != binding.configuration_version):
        raise PluginLoadError(f"{slot}: configuration schema migration required")
    parameters = deepcopy(binding.parameters)
    if descriptor.api_version == "2":
        return loader.instantiate(
            descriptor, expected_base, parameters=parameters,
            context=context or plugin_context(snapshot),
        )
    if parameters:
        raise PluginLoadError(f"{slot}: configurable plugins require constructor API 2")
    return loader.instantiate(descriptor, expected_base)
