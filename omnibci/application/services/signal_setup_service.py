"""Resolve decoder input against the selected or running Acquisition binding."""
from copy import deepcopy
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
from omnibci.domain.signal_info import SignalInfo
from omnibci.domain.channel_mapping import ProcessingInput, compile_processing_plan
from omnibci.shared.events import EventType


def _input_value(snapshot):
    value = processing_input(snapshot)
    return value.to_dict() if value is not None else None


class SignalSetupService:
    def __init__(self, settings, profiles, engine, acquisition, commands):
        self.settings = settings
        self.profiles = profiles
        self.engine = engine
        self.acquisition = acquisition
        self.commands = commands
        self._admission = engine.admission
        self._receipt = None
        self._plan = None
        self._bound_key = None
        self._source_fingerprint = None

    def read(self):
        _, revision = self.settings.read_saved()
        accepted = self.profiles.repository.get_activation()
        source_error = None
        try:
            accepted_settings = self.profiles.materialize(accepted) if accepted else {}
        except ValueError as exc:
            source_error = str(exc)
            bindings = []
            for binding in accepted.bindings:
                value = binding.to_dict()
                value.pop("processing_input", None)
                if binding.processing_input is not None:
                    value["processing_input"] = binding.processing_input.to_dict()
                bindings.append(value)
            accepted_settings = {"PLUGINS.bindings": bindings}
            source_binding = next((item for item in accepted.bindings if item.kind == "acquisition"), None)
            if source_binding is not None:
                accepted_settings["ACQUISITION.signal_source"] = self.profiles.catalog.get(
                    "acquisition", source_binding.selection).plugin_type
        active = self.engine.applied_snapshot if self.engine.is_engine_ready else None
        selected = accepted_settings or active or {}
        decoder = binding_for(selected, "processing.decoder") or {}
        source_binding = binding_for(selected, "acquisition.source")
        metadata = self.acquisition.cached_descriptor_for(source_binding) if active else None
        if metadata is not None:
            try:
                validate_source_binding(selected, SignalInfo.from_dict(metadata))
            except ValueError:
                metadata = None
        receipt = deepcopy(self._receipt)
        # A coordinated narrow Apply may update the revision without changing the input.
        if (active and receipt and self._plan
                and receipt["generation"] == active.get("CONFIG.generation")
                and self._bound_key == processing_binding_key(active)):
            receipt["config_revision"] = active.get("CONFIG.revision")
        ready = bool(not source_error and active and metadata and receipt
                     and self._bound_key == processing_binding_key(selected)
                     and self._bound_key == processing_binding_key(active)
                     and receipt["generation"] == active.get("CONFIG.generation")
                     and receipt["config_revision"] == active.get("CONFIG.revision")
                     and receipt["source_fingerprint"] == SignalInfo.from_dict(metadata).fingerprint
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
                              "metadata_origin": "connected" if metadata else "unavailable"}
        source_revision = sha256(json.dumps({
            "source_id": source_id, "binding": acquisition_binding_key(source_binding),
            "metadata": metadata, "generation": active.get("CONFIG.generation") if active and metadata else None,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"processing_input": _input_value(selected), "revision": revision,
                "selected_source_id": source_id, "selected_decoder_profile_id": decoder.get("profile_id"),
                "selected_decoder_selection": decoder.get("selection"),
                "source_binding": source_context, "source_revision": source_revision,
                "acquisition_descriptor": metadata, "source_error": source_error,
                "accepted_input": _input_value(accepted_settings), "active_input": _input_value(active or {}),
                "readiness": {"ready": ready, "receipt": receipt if ready else None}}

    def _profile(self, binding):
        if binding and binding.get("profile_id"):
            try:
                return self.profiles.repository.get_profile(binding["profile_id"])
            except PluginProfileNotFoundError:
                pass
        return None

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
            if context["acquisition_descriptor"] is None:
                raise ValueError(context["source_error"] or
                                 "Connect the acquisition source and get metadata before previewing processing input")
            source = SignalInfo.from_dict(context["acquisition_descriptor"])
            plan = compile_processing_plan(source, draft)
            self._validate_decoder_channels(decoder_selection or context["selected_decoder_selection"], plan)
            # A source switch while resolving a draft must not publish an obsolete preview.
            current = self.read()
            if context["source_revision"] != current["source_revision"]:
                raise ProfileRevisionConflict("Acquisition source changed; refresh its information and preview again")
            if decoder_selection is None and context["selected_decoder_selection"] != current["selected_decoder_selection"]:
                raise ProfileRevisionConflict("Selected decoder changed; review its requirements and preview again")
            return {"plan": plan.to_dict(), "warnings": [],
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
            metadata = SignalInfo.from_dict(self.acquisition.get_descriptor(
                refresh=not status.streaming))
            source_key = processing_binding_key(snapshot)
            if (self._bound_key == source_key and self._receipt
                    and self._receipt["generation"] == snapshot.get("CONFIG.generation")
                    and self._source_fingerprint != metadata.fingerprint):
                raise ConfigProfileValidationError(
                    "Source metadata changed since preparation; inspect and explicitly update the acquisition or decoder input")
            try:
                validate_source_binding(snapshot, metadata)
                plan = compile_processing_plan(metadata, value, snapshot["CONFIG.revision"])
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
                raise RuntimeError("Processing did not acknowledge the current signal plan")
            self._receipt, self._plan = receipt, plan
            self._bound_key, self._source_fingerprint = source_key, metadata.fingerprint
            return deepcopy(receipt)

    def prepare_session(self):
        receipt = self.prepare()
        return self._plan.to_dict() if receipt is not None else None
