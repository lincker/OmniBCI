"""Resolve the persisted activation once at a standalone process boundary."""
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository


def accepted_runtime_settings(*, profiles=None, active_kinds=None) -> dict:
    """Validate pinned inputs without initializing, refreshing, or saving them."""
    if profiles is None:
        profiles = PluginProfileService(
            TomlPluginProfileRepository(), CatalogConfigurationAdapter(),
            settings_source=lambda: {},
        )
    try:
        accepted = profiles.repository.get_activation()
        if accepted is None:
            raise PluginSchemaError(
                "Plugin configuration is not selected; review and apply profiles before starting"
            )
        if active_kinds and "effector" in active_kinds and not accepted.settings.get("ACTUATION.enabled", False):
            raise PluginSchemaError("Actuation is disabled in the accepted configuration")
        kinds = {"acquisition"} if active_kinds is None else active_kinds
        required = {
            "acquisition": "acquisition.source", "decoder": "processing.decoder",
            "epoch_detector": "processing.epoch_detector", "paradigm": "stimulation.paradigm",
            "effector": "actuation.effector",
        }
        slots = {binding.slot for binding in accepted.bindings}
        missing = {slot for kind, slot in required.items() if kind in kinds and slot not in slots}
        if missing:
            raise PluginSchemaError("Missing required selections: " + ", ".join(sorted(missing)))
        profiles.validate_activation(accepted, active_kinds=kinds)
        return profiles.materialize(accepted)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        raise RuntimeError(f"Plugin configuration is invalid: {exc}") from exc
