"""Read selected processing input, preview it, and prepare the connected path."""
from copy import deepcopy
from dataclasses import replace

from omnibci.application.config_errors import ConfigProfileValidationError
from omnibci.application.plugin_profiles import PluginProfileNotFoundError
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.runtime_admission import admitted
from omnibci.domain.montage import ProcessingInput, SignalSetup, SourceMetadata, compile_signal_plan, merge_source_montage
from omnibci.shared.events import EventType


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
        selected = active or accepted_settings
        decoder = next((item for item in selected.get("PLUGINS.bindings", ())
                        if item["slot"] == "processing.decoder"), {})
        legacy = accepted.settings.get("SIGNAL.setup") if accepted else saved.get("SIGNAL.setup")
        setup = selected.get("SIGNAL.setup") if decoder.get("processing_input") is not None else legacy
        if decoder.get("profile_id"):
            try:
                profile = self.profiles.repository.get_profile(decoder["profile_id"])
            except PluginProfileNotFoundError:
                profile = None
            if profile is not None and profile.processing_input is not None:
                setup = profile.processing_input.as_signal_setup(selected["ACQUISITION.signal_source"]).to_dict()
        metadata = self.acquisition.cached_metadata
        receipt = deepcopy(self._receipt)
        # A coordinated narrow Apply already has both worker revision ACKs.
        # It can preserve this plan only within the same worker generation.
        if (active and receipt and self._plan
                and receipt["generation"] == active.get("CONFIG.generation")
                and active.get("SIGNAL.setup") is not None
                and self._plan.setup == SignalSetup.from_dict(active.get("SIGNAL.setup", {}))):
            receipt["config_revision"] = active.get("CONFIG.revision")
        ready = bool(active and metadata and self._receipt
                     and self._receipt["generation"] == active.get("CONFIG.generation")
                     and receipt["config_revision"] == active.get("CONFIG.revision")
                     and receipt["source_fingerprint"] == SourceMetadata.from_dict(metadata).fingerprint
                     and self._receipt["connection_id"] == metadata.get("connection_id"))
        return {"setup": setup, "revision": revision,
                "selected_source_id": selected.get("ACQUISITION.signal_source"),
                "selected_decoder_profile_id": decoder.get("profile_id"),
                "source_metadata": metadata or self._profile_metadata(selected),
                "legacy_setup": legacy,
                "accepted_setup": accepted_settings.get("SIGNAL.setup"),
                "active_setup": active.get("SIGNAL.setup") if active else None,
                "readiness": {"ready": ready, "receipt": receipt if ready else None}}

    def _profile_metadata(self, snapshot):
        binding = next((item for item in snapshot.get("PLUGINS.bindings", ())
                        if item["slot"] == "acquisition.source"), None)
        if binding is None:
            return None
        manual = binding.get("manual_montage")
        declared = SourceMetadata.from_dict(manual) if manual is not None else None
        observed = None
        if binding.get("profile_id"):
            try:
                profile = self.profiles.repository.get_profile(binding["profile_id"])
            except PluginProfileNotFoundError:
                profile = None
            if (profile is not None and profile.parameters == binding["parameters"]
                    and profile.selection.to_dict() == binding["selection"]):
                observed = profile.acquisition_montage
        if observed is not None and declared is not None:
            try:
                observed = merge_source_montage(observed, declared)
            except ValueError:
                # Keep current source facts visible; preparation reports the conflict.
                pass
        source = observed or declared
        return source.to_dict() if source is not None else None

    def _compile(self, metadata, setup, revision=""):
        if setup.template:
            layout = self.layouts.get(setup.template)
            assignments = {assignment.source_index: assignment.electrode for assignment in setup.assignments}
            channels = []
            for channel in metadata.channels:
                electrode = assignments.get(channel.index, channel.name)
                position = layout["positions"].get(electrode)
                if channel.position is None and position is not None:
                    channel = replace(channel, position=tuple(position), coordinate_frame="head",
                                      position_provenance=layout["provider"])
                channels.append(channel)
            metadata = replace(metadata, channels=tuple(channels))
        return compile_signal_plan(metadata, setup, config_revision=revision)

    def preview_input(self, value, metadata=None):
        """Resolve a decoder draft against the current acquisition montage."""
        try:
            processing_input = ProcessingInput.from_dict(value)
            observed = metadata if metadata is not None else self.read()["source_metadata"]
            if observed is None:
                raise ValueError("Read source information or complete the acquisition profile montage")
            source = SourceMetadata.from_dict(observed)
            setup = processing_input.as_signal_setup(source.source_id)
            plan = self._compile(source, setup)
            return {"plan": plan.to_dict(), "warnings": list(plan.warnings)}
        except (ValueError, TypeError) as exc:
            raise ConfigProfileValidationError(str(exc)) from exc

    @admitted
    def prepare(self):
        with self.acquisition.lifecycle_lock:
            snapshot = self.engine.applied_snapshot
            if not snapshot or snapshot.get("SIGNAL.setup") is None:
                return None
            if not self.engine.is_engine_ready:
                raise RuntimeError("Start the engine before preparing signal processing")
            status = self.acquisition.handle_get_status(GetAcquisitionStatusQuery())
            metadata = SourceMetadata.from_dict(self.acquisition.get_metadata(
                refresh=not status.streaming, allow_manual_fallback=True))
            setup = SignalSetup.from_dict(snapshot["SIGNAL.setup"])
            binding = next((item for item in snapshot["PLUGINS.bindings"]
                            if item["slot"] == "acquisition.source"), None)
            source_key = {"source": binding, "setup": setup.to_dict()}
            if self._bound_key == source_key and self._source_fingerprint != metadata.fingerprint:
                raise ConfigProfileValidationError(
                    "Source metadata changed since preparation; inspect and explicitly update the signal setup")
            expected_rate = snapshot.get("ACQUISITION.sample_rate")
            if expected_rate != metadata.sample_rate:
                raise ConfigProfileValidationError(
                    f"Configured sample rate {expected_rate} differs from source rate {metadata.sample_rate}")
            try:
                plan = self._compile(metadata, setup, snapshot["CONFIG.revision"])
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
