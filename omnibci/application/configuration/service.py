"""Manage Server settings, module configuration and independent plugin profiles."""
from contextlib import nullcontext
from dataclasses import replace
from threading import RLock
from uuid import uuid4

from omnibci.application.configuration.definitions import (
    MODULES, MODULE_SETTING_DEFAULTS, SERVER_SETTINGS, server_startup_settings,
    validate_server_settings,
)
from omnibci.application.configuration.errors import PluginProfileNotFoundError, PluginProfileValidationError, ProfileRevisionConflict
from omnibci.application.configuration.ports import ConfigurationRepository, ProfileVersionCatalog
from omnibci.application.plugin_management.identity import PluginSelection
from omnibci.application.configuration.models import (
    ActivationSet, ModuleActivation, ModuleActivationRevision, EngineSettings, PluginProfile, ProfileBinding,
    ProfileVersionProposal,
    MODULE_KINDS, plugin_kind_for_slot,
)
from omnibci.application.plugin_management.packages import PackageReference, PluginPackageConflictError, PluginPackageError
from omnibci.application.plugin_management.schema import PluginSchemaError, validate_parameters
from omnibci.application.plugin_management.catalog import PluginConfiguration, PluginConfigurationCatalog
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


class ConfigurationService:
    def __init__(self, repository: ConfigurationRepository, catalog: PluginConfigurationCatalog | None = None, *,
                 active_server_settings: dict | None = None, change_guard=None,
                 profile_versions: ProfileVersionCatalog | None = None):
        self.repository = repository
        self.catalog = catalog
        self._profile_versions = profile_versions
        self.change_guard = change_guard or nullcontext
        self._active_server = (server_startup_settings(active_server_settings)
                               if active_server_settings is not None else None)
        self._server_lock = RLock()

    def list_plugin_configurations(self, kind: str) -> list[PluginConfiguration]:
        return self.catalog.list(kind)

    def get_plugin_configuration(self, kind: str, selection: PluginSelection) -> PluginConfiguration:
        return self.catalog.get(kind, selection)

    def selected_configuration(self, modules) -> ActivationSet:
        """Read requested modules and shared Engine settings without touching other modules."""
        modules = tuple(modules)
        if set(modules) - set(MODULES):
            raise PluginSchemaError("Unsupported configuration module")
        return ActivationSet({module: self.repository.get_module(module) for module in modules},
                             self.repository.get_engine())

    def list_profiles(self, kind: str | None = None) -> list[PluginProfile]:
        return self.repository.list_profiles(kind)

    def get_profile(self, profile_id: str) -> PluginProfile:
        return self.repository.get_profile(profile_id)

    def get_module(self, module: str) -> ModuleActivation | None:
        return self.repository.get_module(module)

    def get_engine(self) -> EngineSettings | None:
        return self.repository.get_engine()

    def module_history(self, module: str) -> list[ModuleActivationRevision]:
        return self.repository.list_module_history(module)

    def module_history_entry(self, module: str, entry_id: str) -> ModuleActivationRevision | None:
        return self.repository.get_module_history_entry(module, entry_id)

    def package_references(self) -> tuple[PackageReference, ...]:
        """Project saved and accepted identities without reading unrelated Engine settings."""
        references = [PackageReference("profile", item.profile_id, item.selection)
                      for item in self.repository.list_profiles()]
        for module in MODULES:
            accepted = self.repository.get_module(module)
            if accepted is not None:
                references.extend(PackageReference("accepted", item.slot, item.selection)
                                  for item in accepted.bindings)
        return tuple(references)

    def read_server(self) -> tuple[list[dict], str]:
        with self._server_lock:
            saved, revision = self.repository.read_server()
            values = server_startup_settings(saved)
            if self._active_server is None:
                self._active_server = server_startup_settings(values)
            return [{
                "key": key, "value": values[key], "type": definition.type,
                "requires": "server-restart", "description": definition.description,
                "active_value": self._active_server[key],
                "restart_required": values[key] != self._active_server[key],
            } for key, definition in SERVER_SETTINGS.items()], revision

    def save_server(self, updates: dict, *, expected_revision: str) -> tuple[list[str], bool, str]:
        clean = validate_server_settings(updates)
        with self._server_lock:
            if self._active_server is None:
                self.read_server()
            revision = self.repository.save_server(clean, expected_revision=expected_revision)
            entries, _ = self.read_server()
        return sorted(clean), any(entry["restart_required"] for entry in entries), revision

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

    def propose_profile(self, source_profile_id: str, target_version: str, *,
                        name: str | None = None, parameters: dict | None = None) -> ProfileVersionProposal:
        """Validate a copy against the target schema; preserve all old values by default."""
        if self._profile_versions is None:
            raise PluginPackageError("Version profile metadata is unavailable")
        source = self.repository.get_profile(source_profile_id)
        try:
            selection = replace(source.selection, version=target_version)
        except (ValueError, TypeError) as exc:
            raise PluginProfileValidationError(f"Invalid target version: {exc}") from exc
        if selection == source.selection:
            raise PluginPackageError("Choose a different exact package version")
        package = self._profile_versions.get_package(selection.package_id, target_version)
        if package.error or not package.available:
            raise PluginPackageError(package.error or "Target package is unavailable")
        definition = self._profile_versions.configuration(source.kind, selection)
        if not definition.available or definition.schema is None:
            raise PluginSchemaError("Target contribution requires an available configuration schema")
        values = validate_parameters(definition.schema, source.parameters if parameters is None else parameters)
        # Reuse profile validation for names and portable JSON values without saving anything.
        draft = PluginProfile(source.profile_id, source.name if name is None else name, source.kind, selection,
                              values, definition.configuration_version,
                              processing_input=source.processing_input)
        return ProfileVersionProposal(source.profile_id, source.revision, draft.name, draft.kind,
                                      selection, draft.parameters, draft.configuration_version, package.enabled,
                                      draft.processing_input)

    def create_version_profile(self, source_profile_id: str, target_version: str, *,
                               expected_source_revision: int, name: str | None = None,
                               parameters: dict | None = None) -> PluginProfile:
        """Save a new profile after explicit review; accepted/running bindings stay pinned."""
        with self.change_guard():
            proposal = self.propose_profile(source_profile_id, target_version, name=name, parameters=parameters)
            if proposal.source_revision != expected_source_revision:
                raise ProfileRevisionConflict("Source profile changed; preview the version change again")
            if not proposal.target_enabled:
                raise PluginPackageConflictError("Enable the target package before creating its profile")
            return self.create(proposal.kind, proposal.selection, proposal.name, proposal.parameters,
                               processing_input=proposal.processing_input)

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
        """Initialize an absent module from its declared defaults without storage I/O."""
        return ModuleActivation(module, MODULE_SETTING_DEFAULTS[module], ()).settings

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
                try:
                    selection = PluginSelection.from_dict(item["selection"])
                except (ValueError, TypeError) as exc:
                    raise PluginProfileValidationError(f"Invalid selection: {exc}") from exc
                definition, parameters = self._parameters(
                    kind, selection, item["parameters"], item.get("configuration_version"),
                )
                channel_input = item.get("processing_input")
                if channel_input is not None and kind != "decoder":
                    raise PluginSchemaError("processing_input requires a decoder binding")
                if kind == "decoder":
                    if channel_input is None:
                        raise PluginSchemaError("processing.decoder requires explicit processing_input")
                    try:
                        channel_input = ProcessingInput.from_dict(channel_input)
                    except (ValueError, TypeError) as exc:
                        raise PluginProfileValidationError(f"Invalid processing_input: {exc}") from exc
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

    def propose_engine(self, settings: dict) -> EngineSettings:
        """Validate submitted Engine settings without consulting server startup values."""
        candidate = EngineSettings(settings)
        timeout = candidate.settings.get("NEUROENGINE.startup_timeout_s")
        return (EngineSettings({**candidate.settings, "NEUROENGINE.startup_timeout_s": float(timeout)})
                if type(timeout) is int else candidate)

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
                result.update({f"ACQUISITION.{key}": value for key, value in binding.parameters.items()})
        result["CONFIG.module_revisions"] = {
            module: activation_set.modules[module].revision if activation_set.modules[module] else None
            for module in MODULES
        }
        result["CONFIG.engine_revision"] = (activation_set.engine or EngineSettings({})).revision
        if generation is not None:
            result["CONFIG.generation"] = generation
        result["PLUGINS.bindings"] = bindings
        return result
