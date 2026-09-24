"""Save plugin profiles independently from explicitly accepted startup snapshots."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from omnibci.application.activation_settings import PLUGIN_SETTING_KEYS, activation_host_settings
from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginSelection, ProfileBinding,
    ProfileRevisionConflict,
)
from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters
from omnibci.application.ports.plugin_configuration_catalog import PluginConfigurationCatalog


def _validate_source_metadata(binding: ProfileBinding) -> None:
    """The current engine consumes integer sampling and channel declarations."""
    if binding.slot != "acquisition.source":
        return
    for key in ("sample_rate", "channel_number"):
        value = binding.parameters.get(key)
        if type(value) is not int or value <= 0:
            raise PluginSchemaError(
                f"acquisition.source.parameters.{key} must be a positive integer for the current engine"
            )


class PluginProfileService:
    def __init__(self, repository, catalog: PluginConfigurationCatalog, *,
                 settings_source, change_guard=None):
        self.repository = repository
        self.catalog = catalog
        self.settings_source = settings_source
        self.change_guard = change_guard or (lambda requires_idle: nullcontext())

    def _parameters(self, kind, selection, parameters, configuration_version=None):
        definition = self.catalog.get(kind, selection)
        if definition.schema is None:
            raise PluginSchemaError("This plugin has no editable configuration schema")
        if (configuration_version is not None
                and configuration_version != definition.configuration_version):
            raise PluginSchemaError("The profile requires a configuration schema migration")
        return definition, validate_parameters(definition.schema, parameters)

    def create(self, kind: str, selection: PluginSelection, name: str, parameters: dict):
        with self.change_guard(False):
            definition, values = self._parameters(kind, selection, parameters)
            profile = PluginProfile(
                profile_id=str(uuid4()), name=name, kind=kind, selection=selection,
                parameters=values, configuration_version=definition.configuration_version,
            )
            return self.repository.save_profile(profile, expected_revision=None)

    def update(self, profile_id: str, *, name: str, parameters: dict, expected_revision: int):
        with self.change_guard(False):
            previous = self.repository.get_profile(profile_id)
            _, values = self._parameters(previous.kind, previous.selection, parameters,
                                         previous.configuration_version)
            return self.repository.save_profile(
                replace(previous, name=name, parameters=values), expected_revision=expected_revision,
            )

    def delete(self, profile_id: str, *, expected_revision: int):
        # Accepted bindings include independent values; retain linked profiles to
        # make the user's active selection identifiable until it is replaced.
        with self.change_guard(False):
            active = self.repository.get_activation()
            if active is not None and any(b.profile_id == profile_id for b in active.bindings):
                raise ProfileRevisionConflict("Profile is selected; replace its binding first")
            self.repository.delete_profile(profile_id, expected_revision=expected_revision)

    def propose(self, selections: list[dict], *, settings: dict | None = None,
                refresh_slots: list[str] | None = None) -> ActivationSnapshot:
        """Resolve an ordered set of slot/profile references into pinned copies."""
        current = self.repository.get_activation()
        base = current.settings if current is not None else self.settings_source()
        if settings is not None:
            base = settings
        base = activation_host_settings(base)
        if current is not None:
            # Preserve old inert copies without allowing saved edits to change a running revision.
            base.update({key: value for key, value in current.settings.items()
                         if key in PLUGIN_SETTING_KEYS or key.startswith("LOGGING.")})
        if refresh_slots is not None and (
            not isinstance(refresh_slots, list) or any(not isinstance(slot, str) for slot in refresh_slots)
            or len(set(refresh_slots)) != len(refresh_slots)
            or any(slot not in {item.get("slot") for item in selections if isinstance(item, dict)}
                   for slot in refresh_slots)
        ):
            raise PluginSchemaError("refresh_slots must name unique selected slots")
        previous = {binding.slot: binding for binding in current.bindings} if current else {}
        bindings = []
        for item in selections:
            if not isinstance(item, dict) or set(item) != {"slot", "profile_id"}:
                raise PluginSchemaError("Each binding requires slot and profile_id")
            retained = previous.get(item["slot"])
            if (refresh_slots is not None and item["slot"] not in refresh_slots
                    and retained is not None and retained.profile_id == item["profile_id"]):
                bindings.append(retained)
                continue
            profile = self.repository.get_profile(item["profile_id"])
            definition, parameters = self._parameters(
                profile.kind, profile.selection, profile.parameters, profile.configuration_version,
            )
            bindings.append(ProfileBinding(
                slot=item["slot"], kind=profile.kind, profile_id=profile.profile_id,
                selection=profile.selection, parameters=parameters,
                configuration_version=profile.configuration_version,
                profile_revision=profile.revision,
            ))
        return ActivationSnapshot(bindings=tuple(bindings), settings=base)

    def validate_activation(self, snapshot: ActivationSnapshot, *,
                            active_kinds: set[str] | None = None) -> None:
        """Require a complete configuration and available capabilities used here.

        Stimulation is started separately; its platform-specific selections may
        remain dormant. Callers include currently active outputs when applying.
        """
        slots = {binding.slot for binding in snapshot.bindings}
        required = {"acquisition.source", "processing.decoder", "processing.epoch_detector",
                    "stimulation.paradigm"}
        missing = required - slots
        if missing:
            raise PluginSchemaError("Missing required selections: " + ", ".join(sorted(missing)))
        kinds = {"acquisition", "decoder", "epoch_detector", "filter"} | (active_kinds or set())
        if snapshot.settings.get("ACTUATION.enabled", False):
            if "actuation.effector" not in slots:
                raise PluginSchemaError("Actuation is enabled without an effector selection")
            kinds.add("effector")
        for binding in snapshot.bindings:
            _validate_source_metadata(binding)
            definition, parameters = self._parameters(
                binding.kind, binding.selection, binding.parameters, binding.configuration_version,
            )
            if parameters != binding.parameters:
                raise PluginSchemaError(f"{binding.slot}: snapshot must include resolved defaults")
            if binding.kind in kinds and not definition.available:
                raise PluginSchemaError(f"{binding.slot}: unavailable on this platform")

    def accept(self, snapshot: ActivationSnapshot, *, expected_revision: str | None):
        """Initially idle-only; the runtime coordinator later commits after ACK."""
        with self.change_guard(True):
            self.validate_activation(snapshot)
            return self.repository.save_activation(snapshot, expected_revision=expected_revision)

    def resolve_settings(self) -> dict:
        snapshot = self.repository.get_activation()
        if snapshot is None:
            return deepcopy(self.settings_source())
        return self.materialize(snapshot)

    def materialize(self, snapshot: ActivationSnapshot) -> dict:
        """Project compatibility keys while retaining exact pinned inputs."""
        # Keep host configuration and plugin inputs together through spawn.
        result = activation_host_settings(snapshot.settings)
        aliases = {
            "acquisition.source": "ACQUISITION.signal_source",
            "processing.decoder": "ALGORITHM.decoder",
            "processing.epoch_detector": "ALGORITHM.epoch_detector",
            "stimulation.paradigm": "STIMULATION.paradigm",
            "stimulation.trigger": "ACQUISITION.trigger",
            "actuation.effector": "ACTUATION.effector",
        }
        filters = []
        for binding in snapshot.bindings:
            _validate_source_metadata(binding)
            definition = self.catalog.get(binding.kind, binding.selection)
            if binding.slot in aliases:
                result[aliases[binding.slot]] = definition.plugin_type
            if binding.kind == "acquisition":
                result.update({f"ACQUISITION.{key}": value for key, value in binding.parameters.items()})
            elif binding.kind == "filter":
                filters.append(definition.plugin_type)
        result["ALGORITHM.filters"] = filters
        result["CONFIG.revision"] = snapshot.revision
        result["PLUGINS.bindings"] = [binding.to_dict() for binding in snapshot.bindings]
        return result
