"""Build a complete replacement pipeline without mutating the active pipeline."""

from copy import deepcopy
from dataclasses import dataclass

from omnibci.application.plugin_schema import validate_parameters
from omnibci.domain.ports.algorithm import Decoder, Filter
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.runtime_configuration import (
    configured_bindings, load_configured_plugin,
)
from omnibci.sdk import PluginContext


@dataclass(frozen=True)
class ConfiguredPipeline:
    decoder: Decoder
    filters: tuple[Filter, ...]
    settings_snapshot: dict


def load_filters(snapshot: dict, context: PluginContext) -> list[Filter]:
    """Each ordered filter slot owns a fresh plugin instance."""
    bindings = configured_bindings(snapshot)
    if bindings is not None:
        return [load_configured_plugin(snapshot, binding.slot, "filter", Filter, context=context)
                for binding in bindings if binding.kind == "filter"]
    names = snapshot.get("ALGORITHM.filters", [])
    if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("ALGORITHM.filters must be an ordered list of plugin aliases")
    return [load_configured_plugin(snapshot, f"processing.filter.{index}", "filter", Filter,
                                   legacy_alias=name, context=context)
            for index, name in enumerate(names)]


def _binding_content(binding):
    return (binding.slot, binding.kind, binding.selection,
            binding.configuration_version, binding.parameters)


def _require_trial_changes(schema, before, after, inherited=None):
    if before == after:
        return
    policy = schema.get("x-apply", inherited)
    if schema.get("type") == "object" and isinstance(before, dict) and isinstance(after, dict):
        for key in before.keys() | after.keys():
            _require_trial_changes(schema["properties"][key], before.get(key), after.get(key), policy)
    elif policy != "trial":
        raise ValueError("Changed plugin parameters require a module restart")


def build_pipeline_update(current: dict, candidate: dict, *, sample_rate: int,
                          baseline_duration: float) -> ConfiguredPipeline:
    """Validate fixed context, then construct decoder and filters as one bundle.

    Source, detector, host settings and buffer dimensions remain fixed. Decoder
    and ordered filter selections may change; edits to an existing selection
    must explicitly declare trial-boundary application in its schema.
    """
    if not isinstance(current, dict) or not isinstance(candidate, dict):
        raise ValueError("Pipeline updates require pinned configuration dictionaries")
    snapshot = deepcopy(candidate)
    revision = snapshot.get("CONFIG.revision")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Pipeline update requires a configuration revision")
    previous = configured_bindings(current)
    selected = configured_bindings(snapshot)
    if previous is None or selected is None:
        raise ValueError("Pipeline updates require exact plugin selections")

    flexible = {"decoder", "filter"}
    before_fixed = [_binding_content(binding) for binding in previous if binding.kind not in flexible]
    after_fixed = [_binding_content(binding) for binding in selected if binding.kind not in flexible]
    if before_fixed != after_fixed:
        raise ValueError("Source or detector topology changes require a module restart")
    mirrors = {"PLUGINS.bindings", "CONFIG.revision", "ALGORITHM.decoder", "ALGORITHM.filters"}
    if ({key: value for key, value in current.items() if key not in mirrors}
            != {key: value for key, value in snapshot.items() if key not in mirrors}):
        raise ValueError("Host configuration changes require a module restart")
    if (snapshot.get("ACQUISITION.sample_rate", sample_rate) != sample_rate
            or snapshot.get("ALGORITHM.baseline_duration", baseline_duration) != baseline_duration):
        raise ValueError("Pipeline context differs from the running sample rate or baseline")

    old_slots = {binding.slot: binding for binding in previous}
    catalog = CatalogConfigurationAdapter()
    for binding in selected:
        if binding.kind not in flexible:
            continue
        old = old_slots.get(binding.slot)
        if old is None or old.selection != binding.selection:
            continue
        if old.configuration_version != binding.configuration_version:
            raise ValueError("Plugin schema version changes require a module restart")
        if old.parameters == binding.parameters:
            continue
        definition = catalog.get(binding.kind, binding.selection)
        before = validate_parameters(definition.schema, old.parameters)
        after = validate_parameters(definition.schema, binding.parameters)
        _require_trial_changes(definition.schema, before, after)

    context = PluginContext(sample_rate=sample_rate,
                            channel_names=tuple(snapshot.get("ACQUISITION.channel_names", ())),
                            config_revision=revision)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder, context=context)
    filters = load_filters(snapshot, context)
    return ConfiguredPipeline(decoder, tuple(filters), snapshot)
