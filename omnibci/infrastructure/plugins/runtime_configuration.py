"""Resolve only selected capabilities from a process's pinned configuration."""
from copy import deepcopy

from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.infrastructure.plugins.loader import PluginLoader, PluginLoadError
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.sdk import PluginContext


def configured_bindings(snapshot: dict, *, slot: str | None = None) -> tuple[ProfileBinding, ...]:
    """Require explicit selections, including an intentional empty list."""
    if "PLUGINS.bindings" not in snapshot:
        raise PluginLoadError("Runtime configuration requires pinned PLUGINS.bindings")
    raw = snapshot["PLUGINS.bindings"]
    if not isinstance(raw, list):
        raise PluginLoadError("PLUGINS.bindings must be a list")
    if any(not isinstance(value, dict) or not isinstance(value.get("slot"), str) for value in raw):
        raise PluginLoadError("Each plugin binding must name a slot")
    if len({value["slot"] for value in raw}) != len(raw):
        raise PluginLoadError("Duplicate plugin slot")
    return tuple(ProfileBinding.from_dict(value) for value in raw
                 if slot is None or value["slot"] == slot)


def plugin_context(snapshot: dict, module: str) -> PluginContext:
    return PluginContext(
        sample_rate=snapshot.get("ACQUISITION.sample_rate") if module in {"acquisition", "processing"} else None,
        module_revision=snapshot.get("CONFIG.module_revisions", {}).get(module),
    )


def load_configured_plugin(snapshot: dict, slot: str, kind: str, expected_base,
                           *, context: PluginContext | None = None,
                           catalog: PluginCatalog | None = None, loader=PluginLoader):
    """Use exact accepted selections; never substitute aliases or versions."""
    bindings = configured_bindings(snapshot, slot=slot)
    binding = next((binding for binding in bindings if binding.slot == slot), None)
    if binding is None or binding.kind != kind:
        raise PluginLoadError(f"No {kind} selected for {slot}")
    catalog = catalog or PluginCatalog()
    descriptor = catalog.resolve_selection(kind, binding.selection)
    if descriptor.configuration_version != binding.configuration_version:
        raise PluginLoadError(f"{slot}: configuration schema migration required")
    parameters = deepcopy(binding.parameters)
    context = context or plugin_context(snapshot, slot.split(".", 1)[0])
    if descriptor.channel_requirements is not None:
        if context.input_signal is None:
            raise PluginLoadError(f"{slot}: channel requirements need prepared input; configure the decoder profile input first")
        try:
            descriptor.channel_requirements.validate_channels(context.input_signal.channels)
        except ValueError as exc:
            raise PluginLoadError(f"{slot}: {exc}") from exc
    return loader.instantiate(
        descriptor, expected_base, parameters=parameters, context=context,
    )
