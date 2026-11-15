"""Save plugin profiles independently from explicitly accepted startup snapshots."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from omnibci.application.activation_settings import MODULES, MODULE_SETTING_DEFAULTS, backend_startup_settings, section_settings
from omnibci.application.plugin_profiles import (
    ActivationSet, ModuleActivation, SystemSettings, PluginProfile, PluginSelection, ProfileBinding,
    MODULE_KINDS, PluginProfileNotFoundError, plugin_kind_for_slot,
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
    for key in ("sample_rate", "channel_count"):
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
        self.change_guard = change_guard or nullcontext
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
        """Save an independent named profile with explicit decoder channel input."""
        with self.change_guard():
            definition, values = self._parameters(kind, selection, parameters)
            if processing_input is _UNSET or processing_input is None and kind == "decoder":
                processing_input = ProcessingInput() if kind == "decoder" else None
            profile = PluginProfile(
                profile_id=str(uuid4()), name=name, kind=kind, selection=selection,
                parameters=values, configuration_version=definition.configuration_version,
                processing_input=processing_input,
            )
            return self.repository.save_profile(profile, expected_revision=None)

    def update(self, profile_id: str, *, name: str, parameters: dict, expected_revision: int,
               processing_input=_UNSET):
        with self.change_guard():
            previous = self.repository.get_profile(profile_id)
            _, values = self._parameters(previous.kind, previous.selection, parameters,
                                         previous.configuration_version)
            channel_input = previous.processing_input if processing_input is _UNSET else processing_input
            if previous.kind == "decoder" and processing_input is None:
                channel_input = ProcessingInput()
            return self.repository.save_profile(
                replace(previous, name=name, parameters=values, processing_input=channel_input),
                expected_revision=expected_revision,
            )

    def delete(self, profile_id: str, *, expected_revision: int):
        with self.change_guard():
            self.repository.delete_profile(profile_id, expected_revision=expected_revision)

    def profile_history(self, profile_id: str):
        return self.repository.list_profile_history(profile_id)

    def revert_profile(self, profile_id: str, revision: int, *, expected_revision: int):
        """Restore stored values as a new named-profile revision without applying them."""
        with self.change_guard():
            entry = self.repository.get_profile_revision(profile_id, revision)
            if entry is None:
                raise PluginProfileNotFoundError(f"{profile_id} revision {revision}")
            return self.repository.save_profile(entry.profile, expected_revision=expected_revision)

    def default_module_settings(self, module: str) -> dict:
        """Initialize an absent module's editor from deployment values once."""
        values = {**MODULE_SETTING_DEFAULTS[module], **section_settings(self.settings_source(), module)}
        return ModuleActivation(module, values, ()).settings

    def propose(self, module: str, bindings: list[dict], *, settings: dict,
                refresh_slots: list[str] | None = None) -> ModuleActivation:
        """Replace one module's selections while retaining explicitly pinned profile values."""
        if not isinstance(module, str) or module not in MODULES:
            raise PluginSchemaError("Unsupported configuration module")
        if not isinstance(bindings, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("slot"), str)
            or not item["slot"].startswith(f"{module}.") for item in bindings
        ):
            raise PluginSchemaError("Selections must belong to the requested module")
        current = self.repository.get_module(module)
        if refresh_slots is not None and (
            not isinstance(refresh_slots, list) or any(not isinstance(slot, str) for slot in refresh_slots)
            or len(set(refresh_slots)) != len(refresh_slots)
            or any(slot not in {item["slot"] for item in bindings} for slot in refresh_slots)
        ):
            raise PluginSchemaError("refresh_slots must name unique selected slots")
        previous = {binding.slot: binding for binding in current.bindings} if current else {}
        selected = []
        for item in bindings:
            if ({"slot", "selection", "parameters"} <= set(item)
                    and not set(item) - {"slot", "selection", "parameters", "configuration_version", "processing_input"}):
                kind = plugin_kind_for_slot(item["slot"])
                selection = PluginSelection.from_dict(item["selection"])
                definition, parameters = self._parameters(
                    kind, selection, item["parameters"], item.get("configuration_version"),
                )
                channel_input = item.get("processing_input")
                if channel_input is not None and kind != "decoder":
                    raise PluginSchemaError("processing_input requires a decoder binding")
                if kind == "decoder":
                    if channel_input is None:
                        raise PluginSchemaError("processing.decoder requires explicit processing_input")
                    channel_input = ProcessingInput.from_dict(channel_input)
                selected.append(ProfileBinding(
                    slot=item["slot"], kind=kind, profile_id=None, selection=selection, parameters=parameters,
                    configuration_version=definition.configuration_version, processing_input=channel_input,
                ))
                continue
            if set(item) != {"slot", "profile_id"}:
                raise PluginSchemaError("Each binding requires a saved profile or current plugin values")
            retained = previous.get(item["slot"])
            if (refresh_slots is not None and item["slot"] not in refresh_slots
                    and retained is not None and retained.profile_id == item["profile_id"]):
                selected.append(retained)
                continue
            profile = self.repository.get_profile(item["profile_id"])
            _, parameters = self._parameters(profile.kind, profile.selection, profile.parameters,
                                              profile.configuration_version)
            selected.append(ProfileBinding(
                slot=item["slot"], kind=profile.kind, profile_id=profile.profile_id,
                selection=profile.selection, parameters=parameters,
                configuration_version=profile.configuration_version, profile_revision=profile.revision,
                processing_input=profile.processing_input,
            ))
        candidate = ModuleActivation(module, settings, tuple(selected))
        normalized = {key: float(value) for key, value in candidate.settings.items()
                      if key in {"ALGORITHM.baseline_duration", "ACTUATION.watchdog_timeout_s",
                                 "ACTUATION.max_command_age_s"} and type(value) is int}
        return (ModuleActivation(module, {**candidate.settings, **normalized}, candidate.bindings)
                if normalized else candidate)

    def propose_system(self) -> SystemSettings:
        settings = self.settings_source()
        startup = self._startup_settings_resolver(settings)
        changed = sorted(key for key in startup.keys() | self._startup_settings.keys()
                         if key not in startup or key not in self._startup_settings
                         or startup[key] != self._startup_settings[key])
        if changed:
            raise PluginSchemaError(
                "Backend restart required for " + ", ".join(changed)
                + "; an engine restart does not apply these settings"
            )
        return SystemSettings(section_settings(settings, "system"))

    def validate_module(self, candidate: ModuleActivation, *, active_kinds: set[str] | None = None) -> None:
        """Validate pinned plugin values; completeness belongs to module startup."""
        available = MODULE_KINDS["acquisition"] | MODULE_KINDS["processing"] if active_kinds is None else active_kinds
        for binding in candidate.bindings:
            if binding.kind == "decoder" and binding.processing_input is None:
                raise PluginSchemaError("Configure the decoder channel mapping in Signal Processing")
            _validate_source_parameters(binding)
            definition, parameters = self._parameters(
                binding.kind, binding.selection, binding.parameters, binding.configuration_version,
            )
            if parameters != binding.parameters:
                raise PluginSchemaError(f"{binding.slot}: snapshot must include resolved defaults")
            if binding.kind in available and not definition.available:
                raise PluginSchemaError(f"{binding.slot}: unavailable on this platform")

    def materialize(self, activation_set: ActivationSet, generation: str | None = None) -> dict:
        """Publish pinned sections; only Acquisition supplies shared source facts."""
        result = activation_set.settings()
        bindings = []
        for binding in activation_set.bindings():
            bindings.append(materialize_processing_binding(binding))
            if binding.kind == "acquisition":
                # The display alias is optional for an idle source. Its module
                # validates the pinned plugin when it is applied or started.
                try:
                    definition = self.catalog.get(binding.kind, binding.selection)
                except PluginSchemaError:
                    pass
                else:
                    result["ACQUISITION.signal_source"] = definition.plugin_type
                result.update({f"ACQUISITION.{key}": value for key, value in binding.parameters.items()})
        result["CONFIG.module_revisions"] = {
            module: activation_set.modules[module].revision if activation_set.modules[module] else None
            for module in MODULES
        }
        result["CONFIG.system_revision"] = (activation_set.system or SystemSettings({})).revision
        if generation is not None:
            result["CONFIG.generation"] = generation
        result["PLUGINS.bindings"] = bindings
        return result
