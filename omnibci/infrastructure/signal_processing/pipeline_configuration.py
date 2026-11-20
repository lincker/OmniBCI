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
from omnibci.domain.signal_info import SignalInfo


@dataclass(frozen=True)
class ConfiguredPipeline:
    decoder: Decoder | None
    filters: tuple[Filter, ...]
    settings_snapshot: dict


def load_filters(snapshot: dict, input_signal: SignalInfo) -> list[Filter]:
    """Each ordered filter slot owns a fresh plugin instance."""
    bindings = configured_bindings(snapshot)
    return [load_configured_plugin(snapshot, binding.slot, "filter", Filter, input_signal=input_signal)
            for binding in bindings if binding.kind == "filter"]


def _binding_content(binding):
    return (binding.slot, binding.kind, binding.selection,
            binding.configuration_version, binding.parameters,
            binding.processing_input)


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
                          baseline_duration: float, signal_plan=None, boundary="trial") -> ConfiguredPipeline:
    """Validate fixed context, then construct decoder and filters as one bundle.

    Source, detector, host settings and buffer dimensions remain fixed. Decoder
    and ordered filter selections may change; edits to an existing selection
    must explicitly declare trial-boundary application in its schema.
    """
    if not isinstance(current, dict) or not isinstance(candidate, dict):
        raise ValueError("Pipeline updates require pinned configuration dictionaries")
    snapshot = deepcopy(candidate)
    revision = snapshot.get("CONFIG.module_revisions", {}).get("processing")
    if not isinstance(revision, str) or not revision:
        raise ValueError("Pipeline update requires a configuration revision")
    previous = configured_bindings(current)
    selected = configured_bindings(snapshot)
    if processing_input(current) != processing_input(snapshot):
        raise ValueError("Processing input changes require a module restart")

    flexible = {"decoder", "filter"}
    before_fixed = {binding.slot: _binding_content(binding) for binding in previous
                    if binding.kind in {"acquisition", "epoch_detector"}}
    after_fixed = {binding.slot: _binding_content(binding) for binding in selected
                   if binding.kind in {"acquisition", "epoch_detector"}}
    if before_fixed != after_fixed:
        raise ValueError("Source or detector topology changes require a module restart")
    from omnibci.application.configuration.definitions import SECTION_SETTING_KEYS
    fixed_keys = SECTION_SETTING_KEYS["processing"] | SECTION_SETTING_KEYS["engine"] | SECTION_SETTING_KEYS["acquisition"]
    fixed_keys = fixed_keys | {key for key in current.keys() | snapshot.keys()
                              if key.startswith("ACQUISITION.") and key != "ACQUISITION.trigger"}
    if (any(current.get(key) != snapshot.get(key) for key in fixed_keys)
            or current.get("CONFIG.generation") != snapshot.get("CONFIG.generation")
            or current.get("CONFIG.engine_revision") != snapshot.get("CONFIG.engine_revision")
            or current.get("CONFIG.module_revisions", {}).get("acquisition")
                != snapshot.get("CONFIG.module_revisions", {}).get("acquisition")):
        raise ValueError("Source or Engine context changes require a runtime restart")
    if boundary == "trial":
        before_slots = [(b.slot, b.selection, b.configuration_version) for b in previous if b.kind in flexible]
        after_slots = [(b.slot, b.selection, b.configuration_version) for b in selected if b.kind in flexible]
        if before_slots != after_slots:
            raise ValueError("Plugin topology changes require a processing module boundary")
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
        if boundary == "trial":
            _require_trial_changes(definition.schema, before, after)

    if signal_plan is None:
        return ConfiguredPipeline(None, (), snapshot)

    from .signal_configuration import mapped_input
    input_signal = mapped_input(signal_plan)
    decoder = load_configured_plugin(snapshot, "processing.decoder", "decoder", Decoder, input_signal=input_signal)
    filters = load_filters(snapshot, input_signal)
    return ConfiguredPipeline(decoder, tuple(filters), snapshot)
