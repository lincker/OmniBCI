"""Recoverable import of legacy Acquisition profiles into plugin configuration."""
from __future__ import annotations

from copy import deepcopy

from omnibci.application.configuration import (
    ConfigProfile, validate_parameters as validate_legacy_acquisition_parameters,
)
from omnibci.application.legacy_plugin_config import (
    PluginConfigMigrationError, legacy_parameters, resolve_legacy_configuration,
)
from omnibci.application.plugin_profiles import PluginProfile
from omnibci.application.plugin_schema import validate_parameters
from omnibci.application.services.plugin_config_initialization import (
    InitializationResult as MigrationResult, SettingsPluginConfigInitializer,
)
from omnibci.application.services.plugin_profile_service import PluginProfileService


class LegacyPluginConfigMigration:
    def __init__(self, profile_service: PluginProfileService):
        self.service = profile_service

    def migrate(self, settings_snapshot: dict, legacy_profiles: list[ConfigProfile],
                active_acquisition_id: str | None) -> MigrationResult:
        initializer = SettingsPluginConfigInitializer(self.service)
        existing = initializer.existing()
        if existing is not None:
            return existing
        if not isinstance(settings_snapshot, dict):
            raise PluginConfigMigrationError("settings_snapshot must be an object")
        settings_snapshot = deepcopy(settings_snapshot)
        warnings: list[str] = []
        drafts: list[PluginProfile] = []
        legacy_by_id: dict[str, list[ConfigProfile]] = {}
        for profile in legacy_profiles:
            if not isinstance(profile, ConfigProfile):
                raise PluginConfigMigrationError("legacy_profiles must contain ConfigProfile values")
            legacy_by_id.setdefault(profile.profile_id, []).append(profile)
        if active_acquisition_id is not None and active_acquisition_id not in legacy_by_id:
            raise PluginConfigMigrationError("The active acquisition profile is missing")

        for profile_id, profiles in sorted(legacy_by_id.items()):
            try:
                if len(profiles) != 1:
                    raise PluginConfigMigrationError("Duplicate legacy profile identity")
                profile = profiles[0]
                definition = resolve_legacy_configuration(self.service.catalog, "acquisition", profile.plugin_type)
                legacy_values = validate_legacy_acquisition_parameters(profile.plugin_type, profile.parameters)
                parameters = validate_parameters(definition.schema, legacy_values, apply_defaults=False)
                drafts.append(PluginProfile(
                    profile_id=profile_id, name=profile.name, kind=definition.kind,
                    selection=definition.selection, parameters=parameters,
                    configuration_version=definition.configuration_version,
                ))
            except (ValueError, KeyError, RuntimeError) as exc:
                if profile_id == active_acquisition_id:
                    raise PluginConfigMigrationError(f"Active acquisition profile {profile_id}: {exc}") from exc
                warnings.append(f"Skipped inactive acquisition profile {profile_id}: {exc}")

        return initializer.initialize(
            settings_snapshot, imported_profiles=tuple(drafts),
            active_acquisition_id=active_acquisition_id, import_warnings=tuple(warnings),
        )
