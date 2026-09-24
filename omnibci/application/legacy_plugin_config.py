"""Map legacy plugin selections and parameters without settings or storage I/O."""
from __future__ import annotations

from omnibci.application.plugin_profiles import PluginProfileValidationError
from omnibci.application.plugin_schema import validate_parameters
from omnibci.application.ports.plugin_configuration_catalog import (
    PluginConfiguration, PluginConfigurationCatalog,
)


CCA_PLUGIN_TYPE = "THUBCI_SSVEP_classic40CCA"


class PluginConfigMigrationError(PluginProfileValidationError):
    """Legacy configuration cannot be migrated without changing its meaning."""


def resolve_legacy_configuration(catalog: PluginConfigurationCatalog, kind: str,
                                 alias: str) -> PluginConfiguration:
    """Resolve an alias to the exact version and schema used by legacy settings."""
    if not isinstance(alias, str) or not alias or alias != alias.strip():
        raise PluginConfigMigrationError(f"{kind}: a non-empty plugin alias is required")
    try:
        legacy = catalog.legacy(kind, alias)
        definition = catalog.get(kind, legacy.selection)
    except (ValueError, KeyError, RuntimeError) as exc:
        raise PluginConfigMigrationError(f"Cannot resolve {kind} plugin {alias!r}: {exc}") from exc
    if definition.kind != kind or definition.selection.version is None:
        raise PluginConfigMigrationError(f"{alias}: an exact versioned {kind} selection is required")
    if definition.schema is None or definition.configuration_version is None:
        raise PluginConfigMigrationError(f"{alias}: configuration schema/version is missing")
    return definition


def legacy_parameters(definition: PluginConfiguration, settings_snapshot: dict) -> dict:
    """Read the parameter sources used by legacy plugins, without global state."""
    if definition.schema is None:
        raise PluginConfigMigrationError(f"{definition.plugin_type}: configuration schema is missing")
    values = {}
    if definition.kind in {"acquisition", "trigger", "decoder"}:
        prefix = "ALGORITHM" if definition.kind == "decoder" else "ACQUISITION"
        for key in definition.schema.get("properties", {}):
            source = f"{prefix}.{key}"
            if definition.kind == "decoder" and definition.plugin_type == CCA_PLUGIN_TYPE:
                source = {"window_duration": "ssvep.duration",
                          "offset_time": "ssvep.offset_time"}.get(key, source)
            if source in settings_snapshot:
                values[key] = settings_snapshot[source]
    return validate_parameters(definition.schema, values)
