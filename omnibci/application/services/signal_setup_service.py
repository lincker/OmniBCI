"""Resolve decoder input against the selected or running Acquisition binding."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json

from omnibci.application.config_errors import ConfigProfileValidationError
from omnibci.application.plugin_profiles import PluginProfileNotFoundError, PluginSelection, ProfileRevisionConflict
from omnibci.application.processing_source import (
    acquisition_binding_key, binding_for, processing_binding_key, processing_input,
    validate_source_binding,
)
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.runtime_admission import admitted
from omnibci.domain.montage import (
    ProcessingInput, SourceMetadata, compile_processing_plan, compile_signal_plan, merge_source_montage,
)
from omnibci.shared.events import EventType


def _input_value(snapshot):
    value = processing_input(snapshot)
    return value.to_dict() if isinstance(value, ProcessingInput) else None


def _legacy_view(snapshot):
    """Keep the old read contract without using it to configure processing."""
    value = processing_input(snapshot)
    if isinstance(value, ProcessingInput):
        value = value.as_signal_setup(snapshot["ACQUISITION.signal_source"])
    return value.to_dict() if value is not None else None


class SignalSetupService:
    def __init__(self, settings, profiles, engine, acquisition, commands, layouts):
        self.settings = settings
        self.profiles = profiles
        self.engine = engine
        self.acquisition = acquisition
        self.commands = commands
        self.layouts = layouts
        self._admission = engine.admission
        self._receipt = None
        self._plan = None
        self._bound_key = None
        self._source_fingerprint = None

    def read(self):
        saved, revision = self.settings.read_saved()
        accepted = self.profiles.repository.get_activation()
        accepted_settings = self.profiles.materialize(accepted) if accepted else {}
        active = self.engine.applied_snapshot if self.engine.is_engine_ready else None
        selected = accepted_settings or active or {}
        decoder = binding_for(selected, "processing.decoder") or {}
        source_binding = binding_for(selected, "acquisition.source")
        legacy = accepted.settings.get("SIGNAL.setup") if accepted else saved.get("SIGNAL.setup")
        metadata = self.acquisition.cached_metadata_for(source_binding) if active else None
        if metadata is not None:
            try:
                validate_source_binding(selected, SourceMetadata.from_dict(metadata))
            except ValueError:
                metadata = None
        connected_metadata = metadata
        source_error = None
        if metadata is None:
            try:
                metadata = self._profile_metadata(selected)
            except ConfigProfileValidationError as exc:
                source_error = str(exc)
        receipt = deepcopy(self._receipt)
        # A coordinated narrow Apply may update the revision without changing the input.
        if (active and receipt and self._plan
                and receipt["generation"] == active.get("CONFIG.generation")
                and self._bound_key == processing_binding_key(active)):
            receipt["config_revision"] = active.get("CONFIG.revision")
        ready = bool(active and connected_metadata and receipt
                     and self._bound_key == processing_binding_key(selected)
                     and self._bound_key == processing_binding_key(active)
                     and receipt["generation"] == active.get("CONFIG.generation")
                     and receipt["config_revision"] == active.get("CONFIG.revision")
                     and receipt["source_fingerprint"] == SourceMetadata.from_dict(metadata).fingerprint
                     and receipt["connection_id"] == metadata.get("connection_id"))
        source_id = selected.get("ACQUISITION.signal_source")
        profile = self._profile(source_binding)
        source_context = None
        if source_id is not None:
            same_source = bool(active and source_id == active.get("ACQUISITION.signal_source")
                               and acquisition_binding_key(source_binding) == acquisition_binding_key(
                                   binding_for(active, "acquisition.source")))
            source_context = {"source_id": source_id,
                              "profile_id": source_binding.get("profile_id") if source_binding else None,
                              "profile_name": profile.name if profile else None,
                              "profile_revision": source_binding.get("profile_revision") if source_binding else None,
                              "state": "active" if same_source else "selected",
                              "metadata_origin": "connected" if connected_metadata else "profile" if metadata else "unavailable"}
        source_revision = sha256(json.dumps({
            "source_id": source_id, "binding": acquisition_binding_key(source_binding),
            "metadata": metadata, "generation": active.get("CONFIG.generation") if active and connected_metadata else None,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"setup": _legacy_view(selected) if _input_value(selected) is not None else legacy,
                "processing_input": _input_value(selected), "revision": revision,
                "selected_source_id": source_id, "selected_decoder_profile_id": decoder.get("profile_id"),
                "selected_decoder_selection": decoder.get("selection"),
                "source_binding": source_context, "source_revision": source_revision,
                "source_metadata": metadata, "source_error": source_error, "legacy_setup": legacy,
                "accepted_input": _input_value(accepted_settings), "active_input": _input_value(active or {}),
                "accepted_setup": _legacy_view(accepted_settings), "active_setup": _legacy_view(active or {}),
                "readiness": {"ready": ready, "receipt": receipt if ready else None}}

    def _profile(self, binding):
        if binding and binding.get("profile_id"):
            try:
                return self.profiles.repository.get_profile(binding["profile_id"])
            except PluginProfileNotFoundError:
                pass
        return None

    def _profile_metadata(self, snapshot):
        binding = binding_for(snapshot, "acquisition.source")
        if binding is None:
            return None
        try:
            manual = binding.get("manual_montage")
            declared = SourceMetadata.from_dict(manual) if manual is not None else None
            observed = None
            profile = self._profile(binding)
            if (profile is not None and profile.parameters == binding["parameters"]
                    and profile.selection.to_dict() == binding["selection"]):
                observed = profile.acquisition_montage
            source = merge_source_montage(observed, declared) if observed and declared else observed or declared
            if source is None:
                return None
            validate_source_binding(snapshot, source)
            return replace(source, connection_id="").to_dict()
        except (ValueError, TypeError) as exc:
            raise ConfigProfileValidationError(str(exc)) from exc

    def _compile(self, metadata, value, revision=""):
        if isinstance(value, ProcessingInput):
            return compile_processing_plan(metadata, value, config_revision=revision)
        if value.template:
            layout = self.layouts.get(value.template)
            assignments = {assignment.source_index: assignment.electrode for assignment in value.assignments}
            channels = []
            for channel in metadata.channels:
                electrode = assignments.get(channel.index, channel.name)
                position = layout["positions"].get(electrode)
                if channel.position is None and position is not None:
                    channel = replace(channel, position=tuple(position), coordinate_frame="head",
                                      position_provenance=layout["provider"])
                channels.append(channel)
            metadata = replace(metadata, channels=tuple(channels))
        return compile_signal_plan(metadata, value, config_revision=revision)

    def _validate_decoder_channels(self, selection, plan):
        if selection is None:
            return
        definition = self.profiles.catalog.get("decoder", PluginSelection.from_dict(selection))
        if definition.channel_requirements is not None:
            definition.channel_requirements.validate_channels(plan.output_channels)

    def preview_input(self, value, *, expected_source_revision=None, decoder_selection=None):
        """Preview a decoder draft using only Acquisition-owned source facts."""
        try:
            draft = ProcessingInput.from_dict(value)
            context = self.read()
            if expected_source_revision is not None and expected_source_revision != context["source_revision"]:
                raise ProfileRevisionConflict("Acquisition source changed; refresh its information and preview again")
            if context["source_metadata"] is None:
                raise ValueError(context["source_error"] or
                                 "Read source information or complete the acquisition profile montage")
            source = SourceMetadata.from_dict(context["source_metadata"])
            plan = compile_processing_plan(source, draft)
            self._validate_decoder_channels(decoder_selection or context["selected_decoder_selection"], plan)
            # A source switch while resolving a draft must not publish an obsolete preview.
            current = self.read()
            if context["source_revision"] != current["source_revision"]:
                raise ProfileRevisionConflict("Acquisition source changed; refresh its information and preview again")
            if decoder_selection is None and context["selected_decoder_selection"] != current["selected_decoder_selection"]:
                raise ProfileRevisionConflict("Selected decoder changed; review its requirements and preview again")
            return {"plan": plan.to_dict(), "warnings": list(plan.warnings),
                    "source_revision": context["source_revision"]}
        except (ValueError, TypeError) as exc:
            raise ConfigProfileValidationError(str(exc)) from exc

    @admitted
    def prepare(self):
        with self.acquisition.lifecycle_lock:
            snapshot = self.engine.applied_snapshot
            value = processing_input(snapshot or {})
            if value is None:
                return None
            if not self.engine.is_engine_ready:
                raise RuntimeError("Start the engine before preparing signal processing")
            status = self.acquisition.handle_get_status(GetAcquisitionStatusQuery())
            metadata = SourceMetadata.from_dict(self.acquisition.get_metadata(
                refresh=not status.streaming, allow_manual_fallback=True))
            source_key = processing_binding_key(snapshot)
            if (self._bound_key == source_key and self._receipt
                    and self._receipt["generation"] == snapshot.get("CONFIG.generation")
                    and self._source_fingerprint != metadata.fingerprint):
                raise ConfigProfileValidationError(
                    "Source metadata changed since preparation; inspect and explicitly update the acquisition or decoder input")
            try:
                validate_source_binding(snapshot, metadata)
                plan = self._compile(metadata, value, snapshot["CONFIG.revision"])
                decoder = binding_for(snapshot, "processing.decoder")
                self._validate_decoder_channels(decoder["selection"] if decoder else None, plan)
            except (ValueError, TypeError) as exc:
                raise ConfigProfileValidationError(str(exc)) from exc
            receipt = {"generation": snapshot.get("CONFIG.generation"),
                       "config_revision": snapshot["CONFIG.revision"],
                       "connection_id": metadata.connection_id,
                       "source_fingerprint": metadata.fingerprint,
                       "plan_fingerprint": plan.fingerprint}
            result = self.commands.request(EventType.PrepareSignalProcessing,
                                           {"plan": plan.to_dict(), "receipt": receipt})
            if result.data != receipt:
                raise RuntimeError("Signal Processing did not acknowledge the current signal plan")
            self._receipt, self._plan = receipt, plan
            self._bound_key, self._source_fingerprint = source_key, metadata.fingerprint
            return deepcopy(receipt)

    def prepare_session(self):
        receipt = self.prepare()
        return self._plan.to_dict() if receipt is not None else None
