"""Initialize pinned plugin configuration from an explicit settings snapshot."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from uuid import NAMESPACE_URL, uuid5

from omnibci.application.activation_settings import activation_host_settings
from omnibci.application.legacy_plugin_config import (
    CCA_PLUGIN_TYPE, PluginConfigMigrationError, legacy_parameters, resolve_legacy_configuration,
)
from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginProfileNotFoundError, ProfileBinding,
    ProfileRevisionConflict,
)
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.domain.channel_mapping import ProcessingInput


# Stable identities let interrupted initialization resume without duplicates.
_NAMESPACE = uuid5(NAMESPACE_URL, "urn:omnibci:plugin-config-initialization:v2")
_SLOTS = (
    ("acquisition.source", "acquisition", "ACQUISITION.signal_source", True),
    ("processing.decoder", "decoder", "ALGORITHM.decoder", True),
    ("processing.epoch_detector", "epoch_detector", "ALGORITHM.epoch_detector", True),
    ("stimulation.paradigm", "paradigm", "STIMULATION.paradigm", True),
    ("stimulation.trigger", "trigger", "ACQUISITION.trigger", False),
    ("actuation.effector", "effector", "ACTUATION.effector", False),
)


@dataclass(frozen=True)
class InitializationResult:
    """Accepted startup configuration and any initialization warnings."""
    snapshot: ActivationSnapshot
    warnings: tuple[str, ...]
    already_initialized: bool


class SettingsPluginConfigInitializer:
    def __init__(self, profile_service: PluginProfileService):
        self.service = profile_service

    def existing(self) -> InitializationResult | None:
        """Return accepted configuration without inspecting settings inputs."""
        snapshot = self.service.repository.get_activation()
        if snapshot is None:
            return None
        stored = snapshot.settings.get("CONFIG.initialization_warnings", [])
        warnings = tuple(stored) if isinstance(stored, list) and all(
            isinstance(item, str) for item in stored
        ) else ()
        return InitializationResult(snapshot, warnings, already_initialized=True)

    @staticmethod
    def _binding(slot: str, profile: PluginProfile) -> ProfileBinding:
        return ProfileBinding(slot=slot, kind=profile.kind, profile_id=profile.profile_id,
                              selection=profile.selection, parameters=profile.parameters,
                              configuration_version=profile.configuration_version,
                              profile_revision=max(1, profile.revision),
                              processing_input=profile.processing_input)

    def _ensure_profile(self, draft: PluginProfile) -> PluginProfile:
        try:
            existing = self.service.repository.get_profile(draft.profile_id)
        except PluginProfileNotFoundError:
            return self.service.repository.save_profile(draft, expected_revision=None)
        if replace(existing, revision=0) != draft:
            raise ProfileRevisionConflict(
                f"Initial profile {draft.profile_id} differs from its planned content; "
                "review the existing profile before retrying"
            )
        return existing

    def initialize(self, settings_snapshot: dict) -> InitializationResult:
        """Validate settings before publishing profiles and their activation."""
        existing = self.existing()
        if existing is not None:
            return existing
        if not isinstance(settings_snapshot, dict):
            raise PluginConfigMigrationError("settings_snapshot must be an object")
        settings = deepcopy(settings_snapshot)
        warnings: list[str] = []
        drafts: dict[str, PluginProfile] = {}
        bindings: list[tuple[str, str]] = []

        def add(slot: str, kind: str, alias: str, *, name: str | None = None) -> None:
            definition = resolve_legacy_configuration(self.service.catalog, kind, alias)
            selection = definition.selection
            identity = f"profile:{slot}:{selection.package_id}:{selection.contribution_id}:{selection.version}"
            profile_id = str(uuid5(_NAMESPACE, identity))
            profile = PluginProfile(
                profile_id=profile_id, name=(name or f"Initial {kind}: {definition.label}")[:64],
                kind=definition.kind, selection=selection,
                parameters=legacy_parameters(definition, settings),
                configuration_version=definition.configuration_version,
                processing_input=ProcessingInput() if kind == "decoder" and "SIGNAL.setup" not in settings else None,
            )
            drafts[profile_id] = profile
            bindings.append((slot, profile.profile_id))
            if not definition.available:
                warnings.append(f"{slot}: {definition.selection.package_id} is unavailable on this platform; "
                                "its portable selection was preserved")
            if kind == "decoder" and definition.plugin_type == CCA_PLUGIN_TYPE:
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
                name=f"Initial filter {index + 1}: {alias}"[:64])

        if "SIGNAL.setup" in settings:
            raise PluginConfigMigrationError("Legacy montage configuration must be replaced with a channel mapping before initialization")
        host_settings = activation_host_settings(settings)
        if warnings:
            host_settings["CONFIG.initialization_warnings"] = list(warnings)
        candidate = ActivationSnapshot(host_settings, tuple(self._binding(slot, drafts[profile_id])
                                                            for slot, profile_id in bindings))
        self.service.validate_activation(candidate)
        saved = {profile_id: self._ensure_profile(draft) for profile_id, draft in drafts.items()}
        snapshot = ActivationSnapshot(host_settings, tuple(self._binding(slot, saved[profile_id])
                                                           for slot, profile_id in bindings))
        accepted = self.service.accept(snapshot, expected_revision=None)
        return InitializationResult(accepted, tuple(warnings), already_initialized=False)
