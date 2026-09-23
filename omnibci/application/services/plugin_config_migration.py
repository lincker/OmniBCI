"""Recoverable migration from legacy settings to independent plugin profiles."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, uuid5

from omnibci.application.configuration import (
    ConfigProfile, validate_parameters as validate_legacy_acquisition_parameters,
)
from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginProfileNotFoundError,
    PluginProfileValidationError, ProfileBinding, ProfileRevisionConflict,
)
from omnibci.application.plugin_schema import validate_parameters
from omnibci.application.ports.plugin_configuration_catalog import PluginConfiguration
from omnibci.application.services.plugin_profile_service import PluginProfileService


_NAMESPACE = uuid5(NAMESPACE_URL, "urn:omnibci:plugin-config-migration:v2")
_CCA = "THUBCI_SSVEP_classic40CCA"
_SLOTS = (
    ("acquisition.source", "acquisition", "ACQUISITION.signal_source", True),
    ("processing.decoder", "decoder", "ALGORITHM.decoder", True),
    ("processing.epoch_detector", "epoch_detector", "ALGORITHM.epoch_detector", True),
    ("stimulation.paradigm", "paradigm", "STIMULATION.paradigm", True),
    ("stimulation.trigger", "trigger", "ACQUISITION.trigger", False),
    ("actuation.effector", "effector", "ACTUATION.effector", False),
)


class PluginConfigMigrationError(PluginProfileValidationError):
    """Legacy configuration cannot be migrated without changing its meaning."""


@dataclass(frozen=True)
class MigrationResult:
    snapshot: ActivationSnapshot
    warnings: tuple[str, ...]
    already_migrated: bool


def legacy_parameters(definition: PluginConfiguration, settings_snapshot: dict) -> dict:
    """Read the parameter sources used by legacy plugins, without global state."""
    if definition.schema is None:
        raise PluginConfigMigrationError(f"{definition.plugin_type}: configuration schema is missing")
    values = {}
    if definition.kind in {"acquisition", "trigger", "decoder"}:
        prefix = "ALGORITHM" if definition.kind == "decoder" else "ACQUISITION"
        for key in definition.schema.get("properties", {}):
            source = f"{prefix}.{key}"
            if definition.kind == "decoder" and definition.plugin_type == _CCA:
                source = {"window_duration": "ssvep.duration",
                          "offset_time": "ssvep.offset_time"}.get(key, source)
            if source in settings_snapshot:
                values[key] = settings_snapshot[source]
    return validate_parameters(definition.schema, values)


class LegacyPluginConfigMigration:
    def __init__(self, profile_service: PluginProfileService):
        self.service = profile_service

    def _definition(self, kind: str, alias: str) -> PluginConfiguration:
        if not isinstance(alias, str) or not alias or alias != alias.strip():
            raise PluginConfigMigrationError(f"{kind}: a non-empty plugin alias is required")
        try:
            legacy = self.service.catalog.legacy(kind, alias)
            definition = self.service.catalog.get(kind, legacy.selection)
        except (ValueError, KeyError, RuntimeError) as exc:
            raise PluginConfigMigrationError(f"Cannot resolve {kind} plugin {alias!r}: {exc}") from exc
        if definition.kind != kind or definition.selection.version is None:
            raise PluginConfigMigrationError(f"{alias}: an exact versioned {kind} selection is required")
        if definition.schema is None or definition.configuration_version is None:
            raise PluginConfigMigrationError(f"{alias}: configuration schema/version is missing")
        return definition

    @staticmethod
    def _draft(profile_id: str, name: str, definition: PluginConfiguration,
               parameters: dict) -> PluginProfile:
        return PluginProfile(profile_id=profile_id, name=name, kind=definition.kind,
                             selection=definition.selection, parameters=parameters,
                             configuration_version=definition.configuration_version)

    @staticmethod
    def _binding(slot: str, profile: PluginProfile) -> ProfileBinding:
        return ProfileBinding(slot=slot, kind=profile.kind, profile_id=profile.profile_id,
                              selection=profile.selection, parameters=profile.parameters,
                              configuration_version=profile.configuration_version,
                              profile_revision=max(1, profile.revision))

    def _ensure_profile(self, draft: PluginProfile) -> PluginProfile:
        try:
            existing = self.service.repository.get_profile(draft.profile_id)
        except PluginProfileNotFoundError:
            return self.service.repository.save_profile(draft, expected_revision=None)
        if replace(existing, revision=0) != draft:
            raise ProfileRevisionConflict(
                f"Migrated profile {draft.profile_id} differs from its planned content; "
                "review the existing profile before retrying"
            )
        return existing

    def migrate(self, settings_snapshot: dict, legacy_profiles: list[ConfigProfile],
                active_acquisition_id: str | None) -> MigrationResult:
        existing = self.service.repository.get_activation()
        if existing is not None:
            stored = existing.settings.get("CONFIG.migration_warnings", [])
            warnings = tuple(stored) if isinstance(stored, list) and all(
                isinstance(item, str) for item in stored
            ) else ()
            return MigrationResult(existing, warnings, already_migrated=True)
        if not isinstance(settings_snapshot, dict):
            raise PluginConfigMigrationError("settings_snapshot must be an object")
        settings = deepcopy(settings_snapshot)
        warnings: list[str] = []
        drafts: dict[str, PluginProfile] = {}
        bindings: list[tuple[str, str]] = []
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
                definition = self._definition("acquisition", profile.plugin_type)
                legacy_values = validate_legacy_acquisition_parameters(profile.plugin_type, profile.parameters)
                parameters = validate_parameters(definition.schema, legacy_values, apply_defaults=False)
                drafts[profile_id] = self._draft(profile_id, profile.name, definition, parameters)
            except (ValueError, KeyError, RuntimeError) as exc:
                if profile_id == active_acquisition_id:
                    raise PluginConfigMigrationError(f"Active acquisition profile {profile_id}: {exc}") from exc
                warnings.append(f"Skipped inactive acquisition profile {profile_id}: {exc}")

        def add(slot: str, kind: str, alias: str, *, name: str | None = None) -> None:
            definition = self._definition(kind, alias)
            if slot == "acquisition.source" and active_acquisition_id is not None:
                profile = drafts[active_acquisition_id]
                if profile.selection != definition.selection:
                    raise PluginConfigMigrationError("Active acquisition profile and resolved source disagree")
                expected = legacy_parameters(definition, settings)
                if profile.parameters != expected:
                    raise PluginConfigMigrationError("Active acquisition profile and resolved parameters disagree")
            else:
                selection = definition.selection
                identity = f"profile:{slot}:{selection.package_id}:{selection.contribution_id}:{selection.version}"
                profile_id = str(uuid5(_NAMESPACE, identity))
                profile = self._draft(profile_id, (name or f"Migrated {kind}: {definition.label}")[:64],
                                      definition, legacy_parameters(definition, settings))
                drafts[profile_id] = profile
            bindings.append((slot, profile.profile_id))
            if not definition.available:
                warnings.append(f"{slot}: {definition.selection.package_id} is unavailable on this platform; "
                                "its portable selection was preserved")
            if kind == "decoder" and definition.plugin_type == _CCA:
                for parameter, source in (("window_duration", "ssvep.duration"),
                                          ("offset_time", "ssvep.offset_time")):
                    other = f"ALGORITHM.{parameter}"
                    if other in settings and settings[other] != profile.parameters[parameter]:
                        warnings.append(f"{slot}: retained {source}={profile.parameters[parameter]!r}; "
                                        f"{other}={settings[other]!r} was not used by legacy CCA")

        for slot, kind, key, required in _SLOTS:
            alias = settings.get(key, "")
            if not required and alias == "":
                continue
            add(slot, kind, alias)

        filters = settings.get("ALGORITHM.filters", [])
        if not isinstance(filters, list):
            raise PluginConfigMigrationError("ALGORITHM.filters must be an ordered list")
        for index, alias in enumerate(filters):
            instance_id = uuid5(_NAMESPACE, f"filter:{index}:{alias}")
            add(f"processing.filter.{instance_id}", "filter", alias,
                name=f"Migrated filter {index + 1}: {alias}"[:64])

        if warnings:
            settings["CONFIG.migration_warnings"] = list(warnings)
        candidate = ActivationSnapshot(settings, tuple(self._binding(slot, drafts[profile_id])
                                                       for slot, profile_id in bindings))
        self.service.validate(candidate)
        saved = {profile_id: self._ensure_profile(draft) for profile_id, draft in drafts.items()}
        snapshot = ActivationSnapshot(settings, tuple(self._binding(slot, saved[profile_id])
                                                      for slot, profile_id in bindings))
        accepted = self.service.accept(snapshot, expected_revision=None)
        return MigrationResult(accepted, tuple(warnings), already_migrated=False)
