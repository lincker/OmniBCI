"""Resolve only selected capabilities from a process's pinned configuration."""
from copy import deepcopy

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.sdk import PluginContext


def configured_bindings(snapshot: dict) -> tuple[ProfileBinding, ...] | None:
    """None is legacy configuration; an explicit empty list has no selections."""
    if "PLUGINS.bindings" not in snapshot:
        return None
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
                           *, legacy_key: str | None = None, legacy_alias: str | None = None,
                           context: PluginContext | None = None, legacy_kwargs: dict | None = None,
                           catalog: PluginCatalog | None = None, loader=PluginLoader):
    """Use exact selections when present; never substitute a missing version."""
    catalog = catalog or PluginCatalog()
    bindings = configured_bindings(snapshot)
    if bindings is not None:
        binding = next((binding for binding in bindings if binding.slot == slot), None)
        if binding is None or binding.kind != kind:
            raise PluginLoadError(f"No {kind} selected for {slot}")
        descriptor = catalog.resolve_selection(kind, binding.selection)
        if descriptor.configuration_version != binding.configuration_version:
            raise PluginLoadError(f"{slot}: configuration schema migration required")
        parameters = deepcopy(binding.parameters)
    else:
        from omnibci.application.legacy_plugin_config import legacy_parameters
        alias = legacy_alias if legacy_alias is not None else snapshot.get(legacy_key)
        if not isinstance(alias, str) or not alias:
            raise PluginLoadError(f"No {kind} selected for {slot}")
        descriptor = catalog.resolve_legacy(kind, alias)
        parameters = (legacy_parameters(CatalogConfigurationAdapter._adapt(descriptor), snapshot)
                      if descriptor.api_version == "2" and descriptor.configuration_schema is not None else {})
    if descriptor.api_version == "2":
        return loader.instantiate(
            descriptor, expected_base, parameters=parameters,
            context=context or plugin_context(snapshot),
        )
    if bindings is not None and parameters:
        raise PluginLoadError(f"{slot}: configurable plugins require constructor API 2")
    return loader.instantiate(descriptor, expected_base, **(legacy_kwargs or {}))
