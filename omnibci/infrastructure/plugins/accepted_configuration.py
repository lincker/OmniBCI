"""Resolve accepted module inputs at the Server composition boundary."""
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.configuration.models import MODULE_KINDS


def accepted_runtime_settings(*, profiles, active_kinds=None) -> dict:
    """Validate pinned inputs without initializing, refreshing, or saving them."""
    try:
        kinds = {"acquisition"} if active_kinds is None else active_kinds
        modules = {module for module, supported in MODULE_KINDS.items() if supported & kinds}
        if "processing" in modules:
            modules.add("acquisition")
        accepted = profiles.selected_configuration(modules)
        if accepted.engine is None:
            raise PluginSchemaError("Engine settings are not selected; apply module configuration before starting")
        required = {
            "acquisition": "acquisition.source", "decoder": "processing.decoder",
            "epoch_detector": "processing.epoch_detector", "paradigm": "stimulation.paradigm",
            "effector": "actuation.effector",
        }
        slots = {binding.slot for binding in accepted.bindings()}
        missing = {slot for kind, slot in required.items() if kind in kinds and slot not in slots}
        if missing:
            raise PluginSchemaError("Missing required selections: " + ", ".join(sorted(missing)))
        for module, activation in accepted.modules.items():
            if activation is not None and MODULE_KINDS[module] & kinds:
                profiles.validate_module(activation, active_kinds=kinds)
        return profiles.materialize(accepted)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        raise RuntimeError(f"Plugin configuration is invalid: {exc}") from exc
