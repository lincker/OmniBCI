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
        if active_kinds and "trigger" in active_kinds and not any(
            binding.slot == "stimulation.trigger" for binding in accepted.bindings
        ):
            raise PluginSchemaError("Stimulation requires an accepted trigger selection")
        profiles.validate_activation(accepted, active_kinds=active_kinds)
        return profiles.materialize(accepted)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        raise RuntimeError(f"Plugin configuration is invalid: {exc}") from exc
