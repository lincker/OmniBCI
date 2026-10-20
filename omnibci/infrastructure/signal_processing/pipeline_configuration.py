"""Build a complete replacement pipeline without mutating the active pipeline."""

from copy import deepcopy
from dataclasses import dataclass

from omnibci.application.plugin_schema import validate_parameters
from omnibci.application.processing_source import processing_input, validate_plan_binding
from omnibci.domain.ports.algorithm import Decoder, Filter
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.plugins.runtime_configuration import (
    configured_bindings, load_configured_plugin,
)
from omnibci.sdk import PluginContext


@dataclass(frozen=True)
class ConfiguredPipeline:
    decoder: Decoder | None
    filters: tuple[Filter, ...]
    settings_snapshot: dict


def load_filters(snapshot: dict, context: PluginContext) -> list[Filter]:
    """Each ordered filter slot owns a fresh plugin instance."""
    bindings = configured_bindings(snapshot)
    return [load_configured_plugin(snapshot, binding.slot, "filter", Filter, context=context)
            for binding in bindings if binding.kind == "filter"]


def _binding_content(binding):
    return (binding.slot, binding.kind, binding.selection,
            binding.configuration_version, binding.parameters,
            binding.processing_input)


def validate_output_context(current: dict, candidate: dict) -> dict:
    """Update output-module metadata after a drain without replacing the pipeline."""
    before = configured_bindings(current)
    after = configured_bindings(candidate)
    output_kinds = {"paradigm", "trigger", "effector"}
    if ([_binding_content(b) for b in before if b.kind not in output_kinds]
            != [_binding_content(b) for b in after if b.kind not in output_kinds]):
        raise ValueError("Signal Processing or source changes require a pipeline update")
    mirrors = {"PLUGINS.bindings", "CONFIG.revision", "STIMULATION.paradigm", "ACQUISITION.trigger", "ACTUATION.effector"}
    if ({k: v for k, v in current.items() if k not in mirrors}
            != {k: v for k, v in candidate.items() if k not in mirrors}):
        raise ValueError("Host context changes require a runtime rebuild")
    if not isinstance(candidate.get("CONFIG.revision"), str) or not candidate["CONFIG.revision"]:
        raise ValueError("Runtime context requires a pinned revision")
    return deepcopy(candidate)


def _require_trial_changes(schema, before, after, inherited=None):
    if before == after:
        return
    policy = schema.get("x-apply", inherited)
    if schema.get("type") == "object" and isinstance(before, dict) and isinstance(after, dict):
        for key in before.keys() | after.keys():
            _require_trial_changes(schema["properties"][key], before.get(key), after.get(key), policy)
    elif policy != "trial":
        raise ValueError("Changed plugin parameters require a module restart")


def build_pipeline_update(current: dict, candidate: dict, *, sample_rate: int | None,
                          baseline_duration: float, signal_plan=None) -> ConfiguredPipeline:
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
    if processing_input(current) != processing_input(snapshot):
        raise ValueError("Processing input changes require a module restart")

    flexible = {"decoder", "filter"}
    before_fixed = [_binding_content(binding) for binding in previous if binding.kind not in flexible]
    after_fixed = [_binding_content(binding) for binding in selected if binding.kind not in flexible]
    if before_fixed != after_fixed:
        raise ValueError("Source or detector topology changes require a module restart")
    mirrors = {"PLUGINS.bindings", "CONFIG.revision", "ALGORITHM.decoder", "ALGORITHM.filters"}
    if ({key: value for key, value in current.items() if key not in mirrors}
            != {key: value for key, value in snapshot.items() if key not in mirrors}):
        raise ValueError("Host configuration changes require a module restart")
    if signal_plan is not None:
        validate_plan_binding(snapshot, signal_plan)
        if signal_plan.source.sample_rate != sample_rate:
            raise ValueError("Pipeline context differs from the running sample rate")
    if snapshot.get("ALGORITHM.baseline_duration", baseline_duration) != baseline_duration:
        raise ValueError("Pipeline context differs from the running baseline")

    old_slots = {binding.slot: binding for binding in previous}
    catalog = CatalogConfigurationAdapter()
    for binding in selected:
        if binding.kind not in flexible:
            continue
        definition = catalog.get(binding.kind, binding.selection)
        if not definition.available:
            raise ValueError(f"{binding.slot}: plugin is unavailable on this platform")
        if (definition.configuration_version is not None
                and definition.configuration_version != binding.configuration_version):
            raise ValueError("Plugin configuration schema migration required")
        after = (validate_parameters(definition.schema, binding.parameters)
                 if definition.schema is not None else deepcopy(binding.parameters))
        old = old_slots.get(binding.slot)
        if old is None or old.selection != binding.selection:
            continue
        if old.configuration_version != binding.configuration_version:
            raise ValueError("Plugin schema version changes require a module restart")
        if old.parameters == binding.parameters:
            continue
        if definition.schema is None:
            raise ValueError("Changed plugin parameters require a module restart")
        before = validate_parameters(definition.schema, old.parameters)
        _require_trial_changes(definition.schema, before, after)

    if signal_plan is None:
        return ConfiguredPipeline(None, (), snapshot)

    from .signal_configuration import signal_context
    context = signal_context(snapshot, signal_plan)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder, context=context)
    filters = load_filters(snapshot, context)
    return ConfiguredPipeline(decoder, tuple(filters), snapshot)
