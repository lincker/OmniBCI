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
    ProfileRevisionConflict, PluginProfileNotFoundError,
)
from omnibci.application.plugin_schema import PluginSchemaError, validate_parameters
from omnibci.application.ports.plugin_configuration_catalog import PluginConfigurationCatalog
from omnibci.domain.montage import ProcessingInput, SourceMetadata, merge_source_montage


_UNSET = object()


def _same_parameters(first: dict, second: dict) -> bool:
    return json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def _validate_manual_montage(kind, definition, parameters, manual, observed=None):
    if manual is None:
        return
    if kind != "acquisition" or not isinstance(manual, SourceMetadata):
        raise PluginSchemaError("manual_montage is only available for acquisition profiles")
    if manual.source_id != definition.plugin_type:
        raise PluginSchemaError("Manual montage source_id differs from the selected acquisition plugin")
    if (manual.sample_rate != parameters.get("sample_rate")
            or manual.output_row_count != parameters.get("channel_number")):
        raise PluginSchemaError("Manual montage rate and output rows must match acquisition profile parameters")
    if observed is not None:
        try:
            merge_source_montage(observed, manual)
        except ValueError as exc:
            raise PluginSchemaError(str(exc)) from exc


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
               manual_montage: SourceMetadata | None = None,
               processing_input=_UNSET):
        """Create explicit decoder input by default; None preserves a legacy version copy."""
        with self.change_guard(False):
            definition, values = self._parameters(kind, selection, parameters)
            _validate_manual_montage(kind, definition, values, manual_montage)
            if processing_input is _UNSET:
                processing_input = ProcessingInput() if kind == "decoder" else None
            profile = PluginProfile(
                profile_id=str(uuid4()), name=name, kind=kind, selection=selection,
                parameters=values, configuration_version=definition.configuration_version,
                manual_montage=manual_montage,
                processing_input=processing_input,
            )
            return self.repository.save_profile(profile, expected_revision=None)

    def update(self, profile_id: str, *, name: str, parameters: dict, expected_revision: int,
               manual_montage=_UNSET, processing_input=_UNSET):
        with self.change_guard(False):
            previous = self.repository.get_profile(profile_id)
            definition, values = self._parameters(previous.kind, previous.selection, parameters,
                                         previous.configuration_version)
            manual = previous.manual_montage if manual_montage is _UNSET else manual_montage
            channel_input = previous.processing_input if processing_input is _UNSET else processing_input
            if previous.kind == "decoder" and processing_input is None:
                channel_input = ProcessingInput()
            observed = previous.acquisition_montage if _same_parameters(previous.parameters, values) else None
            _validate_manual_montage(previous.kind, definition, values, manual, observed)
            return self.repository.save_profile(
                replace(previous, name=name, parameters=values, manual_montage=manual,
                        acquisition_montage=observed, processing_input=channel_input),
                expected_revision=expected_revision,
            )

    def record_acquisition_montage(self, binding: ProfileBinding, metadata: SourceMetadata) -> PluginProfile | None:
        """Cache a raw observation only while the saved source still matches.

        Called inside acquisition lifecycle serialization, without taking the
        engine change guard. Concurrent profile edits win the repository CAS.
        """
        if binding.kind != "acquisition" or binding.profile_id is None:
            return None
        if not isinstance(metadata, SourceMetadata):
            raise ValueError("Acquisition montage must be SourceMetadata")
        try:
            profile = self.repository.get_profile(binding.profile_id)
            if (profile.kind != binding.kind or profile.selection != binding.selection
                    or profile.configuration_version != binding.configuration_version
                    or not _same_parameters(profile.parameters, binding.parameters)):
                return None
            observation = replace(metadata, connection_id="")
            if profile.acquisition_montage == observation:
                return profile
            return self.repository.save_profile(
                replace(profile, acquisition_montage=observation), expected_revision=profile.revision,
            )
        except (PluginProfileNotFoundError, ProfileRevisionConflict):
            return None

    def delete(self, profile_id: str, *, expected_revision: int):
        # Accepted bindings include independent values; retain linked profiles to
        # make the user's active selection identifiable until it is replaced.
        with self.change_guard(False):
            active = self.repository.get_activation()
            if active is not None and any(b.profile_id == profile_id for b in active.bindings):
                raise ProfileRevisionConflict("Profile is selected; replace its binding first")
            self.repository.delete_profile(profile_id, expected_revision=expected_revision)

    def propose(self, selections: list[dict], *, include_host_settings: bool = False,
                refresh_slots: list[str] | None = None) -> ActivationSnapshot:
        """Resolve an ordered set of slot/profile references into pinned copies."""
        current = self.repository.get_activation()
        base = self.settings_source() if current is None or include_host_settings else current.settings
        if include_host_settings:
            self._validate_host_changes(base, current)
        base = activation_host_settings(base)
        if current is not None and include_host_settings:
            if "SIGNAL.setup" in current.settings:
                base["SIGNAL.setup"] = current.settings["SIGNAL.setup"]
            else:
                base.pop("SIGNAL.setup", None)
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
                manual_montage=profile.manual_montage,
                processing_input=profile.processing_input,
            ))
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
                            active_kinds: set[str] | None = None) -> None:
        """Require a complete configuration and available capabilities used here.

        Stimulation is started separately; its platform-specific selections may
        remain dormant. Callers include currently active outputs when applying.
        """
        if "SIGNAL.setup" in snapshot.settings:
            from omnibci.domain.montage import SignalSetup
            SignalSetup.from_dict(snapshot.settings["SIGNAL.setup"])
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
            if snapshot.settings.get("ACTUATION.auto_spawn", True):
                kinds.add("effector")
        for binding in snapshot.bindings:
            _validate_source_metadata(binding)
            definition, parameters = self._parameters(
                binding.kind, binding.selection, binding.parameters, binding.configuration_version,
            )
            _validate_manual_montage(binding.kind, definition, parameters, binding.manual_montage)
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
            raise PluginSchemaError("Plugin configuration is not selected; review and apply profiles first")
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
                if binding.manual_montage is not None:
                    result["ACQUISITION.manual_montage"] = binding.manual_montage.to_dict()
            elif binding.kind == "filter":
                filters.append(definition.plugin_type)
        result["ALGORITHM.filters"] = filters
        decoder = next((binding for binding in snapshot.bindings if binding.kind == "decoder"), None)
        if decoder is not None and decoder.processing_input is not None:
            source_id = result.get("ACQUISITION.signal_source")
            if source_id is None:
                raise PluginSchemaError("Decoder processing input requires an acquisition source")
            result["SIGNAL.setup"] = decoder.processing_input.as_signal_setup(source_id).to_dict()
        result["CONFIG.revision"] = snapshot.revision
        result["PLUGINS.bindings"] = [binding.to_dict() for binding in snapshot.bindings]
        return result
