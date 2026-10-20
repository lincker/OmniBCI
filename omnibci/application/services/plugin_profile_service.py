"""Save plugin profiles independently from explicitly accepted startup snapshots."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import json
from uuid import uuid4

from omnibci.application.activation_settings import (
    RUNTIME_HOST_SETTING_KEYS, activation_host_settings, backend_startup_settings,
    is_inert_activation_setting,
)
from omnibci.application.plugin_profiles import (
    ActivationSnapshot, PluginProfile, PluginSelection, ProfileBinding,
    MODULE_KINDS, ProfileRevisionConflict,
)
from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters
from omnibci.application.ports.plugin_configuration_catalog import PluginConfigurationCatalog
from omnibci.application.processing_source import materialize_processing_binding
from omnibci.domain.channel_mapping import ProcessingInput


_UNSET = object()


def _validate_source_parameters(binding: ProfileBinding) -> None:
    """Validate experiment expectations when the source profile declares them."""
    if binding.slot != "acquisition.source":
        return
    for key in ("sample_rate", "channel_number"):
        if key not in binding.parameters:
            continue
        value = binding.parameters.get(key)
        if type(value) is not int or value <= 0:
            raise PluginSchemaError(
                f"acquisition.source.parameters.{key} must be a positive integer"
            )


class PluginProfileService:
    def __init__(self, repository, catalog: PluginConfigurationCatalog, *,
                 settings_source, change_guard=None, startup_settings_resolver=None,
                 loaded_startup_settings=None):
        self.repository = repository
        self.catalog = catalog
        self.settings_source = settings_source
        self.change_guard = change_guard or (lambda requires_idle: nullcontext())
        self._startup_settings_resolver = startup_settings_resolver or backend_startup_settings
        loaded = settings_source() if loaded_startup_settings is None else loaded_startup_settings
        self._startup_settings = deepcopy(self._startup_settings_resolver(loaded))

    def _parameters(self, kind, selection, parameters, configuration_version=None):
        definition = self.catalog.get(kind, selection)
        if definition.schema is None:
            raise PluginSchemaError("This plugin has no editable configuration schema")
        if (configuration_version is not None
                and configuration_version != definition.configuration_version):
            raise PluginSchemaError("The profile requires a configuration schema migration")
        return definition, validate_parameters(definition.schema, parameters)

    def create(self, kind: str, selection: PluginSelection, name: str, parameters: dict, *,
               processing_input=_UNSET):
        """Create explicit decoder input by default; None preserves a legacy version copy."""
        with self.change_guard(False):
            definition, values = self._parameters(kind, selection, parameters)
            if processing_input is _UNSET:
                processing_input = ProcessingInput() if kind == "decoder" else None
            profile = PluginProfile(
                profile_id=str(uuid4()), name=name, kind=kind, selection=selection,
                parameters=values, configuration_version=definition.configuration_version,
                processing_input=processing_input,
            )
            return self.repository.save_profile(profile, expected_revision=None)

    def update(self, profile_id: str, *, name: str, parameters: dict, expected_revision: int,
               processing_input=_UNSET):
        with self.change_guard(False):
            previous = self.repository.get_profile(profile_id)
            _, values = self._parameters(previous.kind, previous.selection, parameters,
                                         previous.configuration_version)
            channel_input = previous.processing_input if processing_input is _UNSET else processing_input
            if previous.kind == "decoder" and processing_input is None:
                channel_input = ProcessingInput()
            return self.repository.save_profile(
                replace(previous, name=name, parameters=values, processing_input=channel_input,
                        legacy=previous.legacy if processing_input is _UNSET and "processing_input" in previous.legacy else {}),
                expected_revision=expected_revision,
            )

    def delete(self, profile_id: str, *, expected_revision: int):
        # Accepted bindings include independent values; retain linked profiles to
        # make the user's active selection identifiable until it is replaced.
        with self.change_guard(False):
            active = self.repository.get_activation()
            if active is not None and any(b.profile_id == profile_id for b in active.bindings):
                raise ProfileRevisionConflict("Profile is selected; replace its binding first")
            self.repository.delete_profile(profile_id, expected_revision=expected_revision)

    def propose(self, selections: list[dict], *, include_host_settings: bool = False,
                refresh_slots: list[str] | None = None, module: str | None = None) -> ActivationSnapshot:
        """Replace owned selections while retaining other modules' pinned values."""
        if module is not None:
            if module not in MODULE_KINDS:
                raise PluginSchemaError("Unsupported configuration module")
            if include_host_settings:
                raise PluginSchemaError("Module configuration cannot change host settings")
            if any(not isinstance(item, dict) or not isinstance(item.get("slot"), str)
                   or not item["slot"].startswith(f"{module}.") for item in selections):
                raise PluginSchemaError("Selections must belong to the requested module")
        current = self.repository.get_activation()
        base = self.settings_source() if current is None or include_host_settings else current.settings
        if include_host_settings:
            self._validate_host_changes(base, current)
        base = activation_host_settings(base)
        if current is not None:
            # Preserve old inert copies without allowing saved edits to change a running revision.
            base.update({key: value for key, value in current.settings.items()
                         if is_inert_activation_setting(key)})
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
            if (isinstance(item, dict) and set(item) == {"slot", "selection", "parameters"}
                    and item["slot"] == "acquisition.source"):
                selection = PluginSelection.from_dict(item["selection"])
                definition, parameters = self._parameters("acquisition", selection, item["parameters"])
                bindings.append(ProfileBinding(
                    slot=item["slot"], kind="acquisition", profile_id=None,
                    selection=selection, parameters=parameters,
                    configuration_version=definition.configuration_version,
                ))
                continue
            if not isinstance(item, dict) or set(item) != {"slot", "profile_id"}:
                raise PluginSchemaError("Each binding requires a saved profile or an acquisition source draft")
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
                processing_input=profile.processing_input,
                legacy={"processing_input": profile.legacy["processing_input"]} if "processing_input" in profile.legacy else {},
            ))
        if module is not None:
            # Keep insertion order for untouched modules and ordered filter chains.
            merged = []
            inserted = False
            for binding in previous.values():
                if binding.slot.startswith(f"{module}."):
                    if not inserted:
                        merged.extend(bindings)
                        inserted = True
                else:
                    merged.append(binding)
            if not inserted:
                merged.extend(bindings)
            bindings = merged
        return ActivationSnapshot(bindings=tuple(bindings), settings=base)

    def _validate_host_changes(self, settings: dict, current: ActivationSnapshot | None) -> None:
        startup = self._startup_settings_resolver(settings)
        changed = sorted(key for key in startup.keys() | self._startup_settings.keys()
                         if key not in startup or key not in self._startup_settings
                         or startup[key] != self._startup_settings[key])
        if changed:
            raise PluginSchemaError(
                "Backend restart required for " + ", ".join(changed)
                + "; an engine restart does not apply these settings"
            )
        if current is not None:
            before = activation_host_settings(current.settings)
            after = activation_host_settings(settings)
            unknown = (before.keys() | after.keys()) - RUNTIME_HOST_SETTING_KEYS
            canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"),
                                                  ensure_ascii=False, allow_nan=False)
            try:
                changed = sorted(key for key in unknown
                                 if key not in before or key not in after
                                 or canonical(before[key]) != canonical(after[key]))
            except (TypeError, ValueError, RecursionError) as exc:
                raise PluginSchemaError("Unclassified host settings must contain finite JSON values") from exc
            if changed:
                raise PluginSchemaError(
                    "Unclassified host settings cannot be changed: " + ", ".join(changed)
                    + "; use a Plugin Profile or declare their host ownership first"
                )

    def validate_activation(self, snapshot: ActivationSnapshot, *,
                            active_kinds: set[str] | None = None,
                            module: str | None = None) -> None:
        """Validate owned selections; completeness belongs to module startup."""
        if module is not None and module not in MODULE_KINDS:
            raise PluginSchemaError("Unsupported configuration module")
        owned = MODULE_KINDS[module] if module is not None else active_kinds
        available = active_kinds if active_kinds is not None else MODULE_KINDS["acquisition"] | MODULE_KINDS["processing"]
        for binding in snapshot.bindings:
            if owned is not None and binding.kind not in owned:
                continue
            if binding.processing_input_requires_review:
                raise PluginSchemaError("Configure the decoder channel mapping in Signal Processing")
            _validate_source_parameters(binding)
            definition, parameters = self._parameters(
                binding.kind, binding.selection, binding.parameters, binding.configuration_version,
            )
            if parameters != binding.parameters:
                raise PluginSchemaError(f"{binding.slot}: snapshot must include resolved defaults")
            if binding.kind in available and not definition.available:
                raise PluginSchemaError(f"{binding.slot}: unavailable on this platform")

    def accept(self, snapshot: ActivationSnapshot, *, expected_revision: str | None,
               module: str | None = None):
        """Initially idle-only; the runtime coordinator later commits after ACK."""
        with self.change_guard(True):
            self.validate_activation(snapshot, module=module)
            return self.repository.save_activation(snapshot, expected_revision=expected_revision)

    def resolve_settings(self) -> dict:
        snapshot = self.repository.get_activation()
        if snapshot is None:
            raise PluginSchemaError("Plugin configuration is not selected; review and apply profiles first")
        return self.materialize(snapshot)

    def materialize(self, snapshot: ActivationSnapshot) -> dict:
        """Publish pinned inputs; only Acquisition supplies shared source facts."""
        result = activation_host_settings(snapshot.settings)
        bindings = []
        for binding in snapshot.bindings:
            bindings.append(materialize_processing_binding(binding))
            if binding.kind == "acquisition":
                _validate_source_parameters(binding)
                definition = self.catalog.get(binding.kind, binding.selection)
                result["ACQUISITION.signal_source"] = definition.plugin_type
                result.update({f"ACQUISITION.{key}": value for key, value in binding.parameters.items()})
        result["CONFIG.revision"] = snapshot.revision
        result["PLUGINS.bindings"] = bindings
        return result
