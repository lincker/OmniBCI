"""Processing attaches a validated plan without owning source reading."""
from omnibci.application.source_metadata import source_descriptor, source_fingerprint, descriptor_layout
from omnibci.domain.ports.acquisition import SourceObservation
from copy import deepcopy
from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

import pytest

from omnibci.application.configuration.errors import ConfigProfileValidationError
from omnibci.application.events import StreamDrainReceipt
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.configuration.models import ActivationSet, ModuleActivation
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.shared.events import Event, EventType


@pytest.fixture
def signal_service(tmp_path):
    metadata = source_descriptor(SourceObservation(native_text="native", signal_info=SignalInfo(sample_rate=500, channels=(
        ChannelDescriptor(0, "O1", unit="uV", channel_id="channel-0"), ChannelDescriptor(1, "M1", unit="uV", channel_id="channel-1"),
    )), native_event="final-row", provenance="protocol-defined"), source_id="source", connection_id="connection")
    setup = ProcessingInput(outputs=(ChannelBinding("O1", "channel-0"),)).to_dict()
    snapshot = { "CONFIG.module_revisions": {"acquisition": "a" * 64, "processing": "p" * 64},
                "CONFIG.engine_revision": "s" * 64,
                "CONFIG.generation": "generation", "ACQUISITION.sample_rate": 500,
                "ACQUISITION.signal_source": "source", "PLUGINS.bindings": [
                    {"slot": "processing.decoder", "processing_input": setup,
                     "selection": {"package_id": "test.decoder", "contribution_id": "default", "version": "1"}},
                    {"slot": "processing.epoch_detector", "selection": {
                        "package_id": "test.detector", "contribution_id": "default", "version": "1"}}]}
    engine = SimpleNamespace(admission=RuntimeAdmission(), is_engine_ready=True, applied_snapshot=snapshot,
                             is_module_running=lambda module: module in ("acquisition", "processing"))
    profiles = SimpleNamespace(selected_configuration=lambda modules: ActivationSet({}, None),
                               materialize=lambda activation: {},
                               get_plugin_configuration=lambda *args: SimpleNamespace(channel_requirements=None))
    acquisition = SimpleNamespace(lifecycle_lock=RLock(), cached_descriptor=deepcopy(metadata),
                                  handle_get_status=lambda query: SimpleNamespace(streaming=False, connected=True))
    acquisition.cached_descriptor_for = lambda binding: deepcopy(acquisition.cached_descriptor)
    acquisition.get_descriptor = lambda **kwargs: deepcopy(acquisition.cached_descriptor)
    calls = []

    def request(event, payload):
        calls.append((event, deepcopy(payload)))
        if event is EventType.PrepareSignalProcessing:
            return SimpleNamespace(data=deepcopy(payload["receipt"]))
        if event is EventType.AttachProcessingInput:
            return SimpleNamespace(data={**deepcopy(payload), "attached": True, "last_sequence": 0})
        if event is EventType.DetachProcessingInput:
            current = engine.applied_snapshot
            return SimpleNamespace(data=StreamDrainReceipt(
                current["CONFIG.generation"], current["CONFIG.module_revisions"]["acquisition"],
                0, 0, False, engine_revision=current["CONFIG.engine_revision"],
                processing_revision=current["CONFIG.module_revisions"]["processing"] if payload.get("drain", True) else None,
            ).to_dict())
        if event is EventType.ResetProcessingInput:
            return SimpleNamespace(data={"attached": False})
        raise AssertionError(f"Unexpected command: {event}")

    service = SignalSetupService(profiles, engine, acquisition,
                                SimpleNamespace(request=request))
    return service, metadata, setup, calls


@pytest.mark.parametrize("source", [{"source_channel_id": "channel-0"}, {"source_channel_index": 0}])
def test_disconnected_mapping_preview_requires_metadata_from_the_current_connection(signal_service, source):
    service, _, _, calls = signal_service
    service.acquisition.cached_descriptor = None
    assert service.read()["acquisition_descriptor"] is None
    assert not service.read()["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError, match="Connect the acquisition source and get metadata"):
        service.preview_input({"outputs": [{**source, 'name': 'O1'}]})
    assert not calls


def test_preparation_uses_current_observation_and_checks_acknowledgement(signal_service):
    service, metadata, setup, calls = signal_service
    receipt = service.prepare()
    assert calls[0][0] is EventType.PrepareSignalProcessing
    assert calls[0][1]["settings_snapshot"] == service.engine.applied_snapshot
    assert receipt["connection_id"] == metadata["connection_id"]
    assert receipt["source_fingerprint"] == source_fingerprint(metadata)
    assert service.read()["readiness"]["ready"]
    assert service.prepare() == receipt
    assert len(calls) == 2
    service.reset()
    service.commands.request = lambda event, payload: SimpleNamespace(data={})
    with pytest.raises(RuntimeError, match="acknowledge"):
        service.prepare()


def test_reconnect_requires_new_receipt_and_source_drift_blocks_prepare(signal_service):
    service, metadata, _, calls = signal_service
    service.prepare()
    service.acquisition.cached_descriptor = None
    assert not service.read()["readiness"]["ready"]
    reconnected = {**metadata, 'connection_id': "new-connection"}
    service.acquisition.cached_descriptor = deepcopy(reconnected)
    assert not service.read()["readiness"]["ready"]
    assert service.prepare()["connection_id"] == "new-connection"
    service.acquisition.cached_descriptor = {**reconnected, 'channels': ({**metadata["channels"][0], 'name': "O2"}, metadata["channels"][1])}
    assert not service.read()["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError, match="metadata changed"):
        service.prepare()
    assert len([event for event, _ in calls if event is EventType.PrepareSignalProcessing]) == 2


def test_known_rate_conflict_blocks_before_processor_command(signal_service):
    service, metadata, _, calls = signal_service
    service.acquisition.cached_descriptor = {**metadata, 'sample_rate': 1000}
    with pytest.raises(ConfigProfileValidationError, match="sample rate"):
        service.prepare()
    assert not calls


def test_stopped_processing_does_not_prepare_or_report_stale_readiness(signal_service):
    service, _, _, calls = signal_service
    service.prepare()
    assert service.read()["readiness"]["ready"]
    service.engine.is_module_running = lambda module: False
    assert service.prepare() is None
    assert not service.read()["readiness"]["ready"]
    assert len(calls) == 2


def test_reset_requires_a_new_receipt_after_processing_restarts(signal_service):
    service, _, _, calls = signal_service
    service.prepare()
    service.reset()
    assert not service.read()["readiness"]["ready"]
    service.prepare()
    assert service.read()["readiness"]["ready"]
    assert len(calls) == 4


def test_invalid_preview_does_not_write_or_prepare(signal_service):
    service, metadata, setup, calls = signal_service
    with pytest.raises(ConfigProfileValidationError, match="Missing acquisition channel"):
        service.preview_input({"outputs": [{'source_channel_id': 'missing', 'name': 'missing'}]})
    assert "setup" not in service.read()
    assert not calls


@pytest.mark.parametrize("source", [{"source_channel_id": "channel-0"}, {"source_channel_index": 0}])
def test_processing_input_preview_uses_source_facts_without_writing(signal_service, source):
    service, metadata, _, calls = signal_service
    before = service.read()
    result = service.preview_input({"outputs": [{**source, 'name': 'O1'}]})
    assert result["plan"]["source_indices"] == [0]
    assert result["plan"]["source"] == deepcopy(metadata)
    assert service.read() == before
    assert not calls


@pytest.mark.parametrize("value", [
    {"source_id": "different"}, {"assignments": []}, {"original_reference": "M2"},
    {"manual_metadata": {}}, {"template": "template"},
])
def test_processing_input_cannot_redeclare_acquisition_metadata(signal_service, value):
    service, _, _, calls = signal_service
    with pytest.raises(ConfigProfileValidationError):
        service.preview_input(value)
    assert not calls


def test_decoder_input_read_uses_pinned_input_without_saved_draft(signal_service):
    from uuid import uuid4
    from omnibci.application.plugin_management.identity import PluginSelection
    from omnibci.application.configuration.models import PluginProfile, ProfileBinding
    from omnibci.domain.channel_mapping import ProcessingInput
    service, _, _, _ = signal_service
    selection = PluginSelection("test.decoder", "default", "1")
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder", selection, {}, revision=1,
                            processing_input=ProcessingInput(outputs=(ChannelBinding('O1', 'channel-0'),)))
    binding = ProfileBinding("processing.decoder", "decoder", profile.profile_id, selection,
                             {}, profile_revision=1, processing_input=profile.processing_input)
    accepted = ActivationSet({"processing": ModuleActivation("processing", {}, (binding,))}, None)
    service.profiles.selected_configuration = lambda modules: accepted
    service.profiles.get_profile = lambda profile_id: profile
    service.profiles.materialize = lambda activation: {        "PLUGINS.bindings": [binding.to_dict()], "ACQUISITION.signal_source": "source"}
    service.engine.applied_snapshot["PLUGINS.bindings"] = [binding.to_dict()]
    service.engine.applied_snapshot["PLUGINS.bindings"][0]["processing_input"] = ProcessingInput(outputs=(ChannelBinding('M1', 'channel-1'),)).to_dict()
    profile = replace(profile, processing_input=ProcessingInput(outputs=(ChannelBinding('M1', 'channel-1'),)))
    result = service.read()
    assert result["selected_decoder_profile_id"] == profile.profile_id
    assert result["processing_input"]["outputs"] == [{"source_channel_id": "channel-0", "name": "O1"}]
    assert result["accepted_input"]["outputs"] == [{"source_channel_id": "channel-0", "name": "O1"}]
    assert result["active_input"]["outputs"] == [{"source_channel_id": "channel-1", "name": "M1"}]
    assert not {"setup", "accepted_setup", "active_setup", "legacy_setup"} & result.keys()


def modern_binding(service, metadata, *, profile_id="source-a", outputs=("O1",)):
    from omnibci.domain.channel_mapping import ProcessingInput
    source = {"slot": "acquisition.source", "profile_id": profile_id,
              "profile_revision": 1, "selection": {"plugin_id": "test.source"},
              "parameters": {"sample_rate": 500, "channel_count": 3}}
    snapshot = {**service.engine.applied_snapshot,
                "PLUGINS.bindings": [source, {"slot": "processing.decoder",
                    "selection": {"package_id": "test.decoder", "contribution_id": "default", "version": "1"},
                    "processing_input": ProcessingInput(outputs=tuple(ChannelBinding(name, {"O1": "channel-0", "M1": "channel-1"}[name]) for name in outputs)).to_dict()},
                    {"slot": "processing.epoch_detector", "selection": {
                        "package_id": "test.detector", "contribution_id": "default", "version": "1"}}]}
    snapshot.pop("SIGNAL.setup", None)
    service.profiles.get_profile = lambda profile_id: None
    service.engine.applied_snapshot = deepcopy(snapshot)
    service.profiles.materialize = lambda activation: deepcopy(snapshot)
    return snapshot


def test_preview_uses_edited_decoder_requirements_without_changing_selected_input(signal_service):
    from omnibci.domain.decoder_input import DecoderChannelRequirements
    service, metadata, _, calls = signal_service
    snapshot = modern_binding(service, metadata)
    before = deepcopy(snapshot)
    edited = {"package_id": "test.edited", "contribution_id": "default", "version": "1"}
    service.profiles.get_plugin_configuration = lambda kind, selection: SimpleNamespace(
        channel_requirements=DecoderChannelRequirements(
            required_names=("M1",) if selection.package_id == "test.edited" else ("O1",)))

    assert service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]})["plan"]["output_channels"][0]["name"] == "O1"
    with pytest.raises(ConfigProfileValidationError, match="M1"):
        service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]}, decoder_selection=edited)
    plan = service.preview_input({"outputs": [{'source_channel_id': 'channel-1', 'name': 'M1'}]}, decoder_selection=edited)["plan"]
    assert plan["output_channels"][0]["name"] == "M1"
    assert service.engine.applied_snapshot == before
    assert not calls


def test_preparation_rejects_missing_algorithm_channel_before_worker_command(signal_service):
    from omnibci.domain.decoder_input import DecoderChannelRequirements
    service, metadata, _, calls = signal_service
    modern_binding(service, metadata)
    service.profiles.get_plugin_configuration = lambda *args: SimpleNamespace(
        channel_requirements=DecoderChannelRequirements(required_names=("M1",)))
    with pytest.raises(ConfigProfileValidationError, match="M1"):
        service.prepare()
    assert not calls


def test_modern_preparation_uses_native_input_without_global_setup(signal_service):
    service, metadata, _, calls = signal_service
    snapshot = modern_binding(service, metadata)
    service.prepare()
    plan = [payload for event, payload in calls if event is EventType.PrepareSignalProcessing][-1]["plan"]
    assert "SIGNAL.setup" not in snapshot
    assert "setup" not in plan
    assert plan["processing_input"]["outputs"] == [{"source_channel_id": "channel-0", "name": "O1"}]
    assert plan["source"] == descriptor_layout(metadata).to_dict()
    assert service.read()["readiness"]["ready"]
    assert len(calls) == 2


def test_source_switch_waits_for_connected_metadata_and_recompiles_mapping(signal_service):
    from omnibci.application.processing_source import acquisition_binding_key
    service, metadata, _, calls = signal_service
    selected = modern_binding(service, metadata)
    owner = acquisition_binding_key(selected["PLUGINS.bindings"][0])
    service.acquisition.cached_descriptor_for = lambda binding: (
        deepcopy(service.acquisition.cached_descriptor) if acquisition_binding_key(binding) == owner else None)
    service.prepare()
    before = service.read()
    reordered = {**metadata, 'connection_id': "new", 'channels': (
        {**metadata["channels"][1], 'index': 0}, {**metadata["channels"][0], 'index': 1})}
    selected["PLUGINS.bindings"][0]["profile_id"] = "source-b"
    selected["PLUGINS.bindings"][0]["parameters"]["data_file_path"] = "another-recording.cnt"
    selected["CONFIG.module_revisions"]["processing"] = "b" * 64
    after = service.read()
    assert after["source_binding"]["profile_id"] == "source-b"
    assert after["source_binding"]["state"] == "selected"
    assert after["source_binding"]["metadata_origin"] == "unavailable"
    assert after["acquisition_descriptor"] is None
    assert before["source_revision"] != after["source_revision"]
    assert not after["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError, match="Connect the acquisition source and get metadata"):
        service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]})
    # Runtime preparation keeps the applied source until the new selection is applied.
    service.prepare()
    assert [payload for event, payload in calls if event is EventType.PrepareSignalProcessing][-1]["plan"]["source_indices"] == [0]
    service.engine.applied_snapshot = deepcopy(selected)
    service.acquisition.cached_descriptor = deepcopy(reordered)
    owner = acquisition_binding_key(selected["PLUGINS.bindings"][0])
    service.prepare()
    assert [payload for event, payload in calls if event is EventType.PrepareSignalProcessing][-1]["plan"]["source_indices"] == [1]
    assert service.read()["readiness"]["ready"]


def test_current_source_with_unchanged_values_preserves_metadata_and_preparation(signal_service):
    from omnibci.application.processing_source import acquisition_binding_key
    service, metadata, _, calls = signal_service
    selected = modern_binding(service, metadata)
    owner = acquisition_binding_key(selected["PLUGINS.bindings"][0])
    service.acquisition.cached_descriptor_for = lambda binding: (
        deepcopy(service.acquisition.cached_descriptor) if acquisition_binding_key(binding) == owner else None)
    service.prepare()
    before = service.read()
    selected["PLUGINS.bindings"][0].pop("profile_id")
    selected["PLUGINS.bindings"][0]["profile_revision"] = 0
    current = service.read()
    assert service.engine.applied_snapshot["PLUGINS.bindings"][0]["profile_id"] == "source-a"
    assert current["source_binding"]["profile_id"] is None
    assert current["source_binding"]["state"] == "active"
    assert current["acquisition_descriptor"] == deepcopy(metadata)
    assert current["source_revision"] == before["source_revision"]
    assert current["readiness"] == before["readiness"]
    assert current["readiness"]["ready"]
    assert len(calls) == 2


def test_stale_preview_revision_rejected_after_reconnect(signal_service):
    from omnibci.application.configuration.errors import ProfileRevisionConflict
    service, metadata, _, calls = signal_service
    modern_binding(service, metadata)
    revision = service.read()["source_revision"]
    service.acquisition.cached_descriptor = {**metadata, 'connection_id': "new-connection"}
    with pytest.raises(ProfileRevisionConflict, match="source changed"):
        service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]}, expected_source_revision=revision)
    assert not calls
    context = service.read()
    preview = service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]}, expected_source_revision=context["source_revision"])
    assert preview["source_revision"] == context["source_revision"]


def test_metadata_revision_update_preserves_prepared_binding_but_generation_does_not(signal_service):
    service, metadata, _, _ = signal_service
    selected = modern_binding(service, metadata)
    service.prepare()
    before = service.read()
    selected["PLUGINS.bindings"][0]["profile_revision"] += 1
    service.engine.applied_snapshot = deepcopy(selected)
    assert service.read()["readiness"]["ready"]
    assert service.read()["source_revision"] == before["source_revision"]
    service.engine.applied_snapshot["CONFIG.generation"] = "next-generation"
    assert not service.read()["readiness"]["ready"]
    assert service.read()["source_revision"] != before["source_revision"]


def test_modern_preparation_rejects_foreign_source_before_worker_command(signal_service):
    service, metadata, _, calls = signal_service
    modern_binding(service, metadata)
    service.acquisition.cached_descriptor = {**metadata, 'source_id': "other-device"}
    with pytest.raises(ConfigProfileValidationError, match="different acquisition source"):
        service.prepare()
    assert not calls


def test_saved_profile_metadata_cannot_supply_disconnected_source_facts(signal_service):
    service, metadata, _, _ = signal_service
    snapshot = modern_binding(service, metadata)
    service.acquisition.cached_descriptor = None
    service.profiles.get_profile = lambda profile_id: SimpleNamespace(
        name="Conflicting source",
        parameters=snapshot["PLUGINS.bindings"][0]["parameters"],
        selection=SimpleNamespace(to_dict=lambda: snapshot["PLUGINS.bindings"][0]["selection"]),
        legacy={"acquisition_montage": {"original_reference": "M2"}})
    context = service.read()
    assert context["acquisition_descriptor"] is None
    assert context["source_binding"]["profile_name"] == "Conflicting source"
    assert context["source_binding"]["metadata_origin"] == "unavailable"
    assert context["source_error"] is None
    with pytest.raises(ConfigProfileValidationError, match="Connect the acquisition source"):
        service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]})


def test_explicit_runtime_rebuild_resolves_changed_source_observation(signal_service):
    service, metadata, _, calls = signal_service
    selected = modern_binding(service, metadata, outputs=())
    service.prepare()
    changed = {**metadata, 'connection_id': "next-connection", 'channels': (
        {**metadata["channels"][0], 'name': "O2"}, metadata["channels"][1])}
    service.acquisition.cached_descriptor = deepcopy(changed)
    with pytest.raises(ConfigProfileValidationError, match="metadata changed"):
        service.prepare()
    selected["PLUGINS.bindings"][0]["profile_revision"] += 1
    selected["CONFIG.generation"] = "rebuilt"
    service.engine.applied_snapshot = deepcopy(selected)
    service.prepare()
    assert [payload for event, payload in calls if event is EventType.PrepareSignalProcessing][-1]["plan"]["output_channels"][0]["name"] == "O2"
    assert service.read()["readiness"]["ready"]


def test_stopped_engine_discards_source_facts_even_if_old_cache_survives(signal_service):
    service, metadata, _, _ = signal_service
    modern_binding(service, metadata)
    service.engine.is_engine_ready = False
    context = service.read()
    assert context["source_binding"]["state"] == "selected"
    assert context["source_binding"]["metadata_origin"] == "unavailable"
    assert context["acquisition_descriptor"] is None
    assert not context["readiness"]["ready"]


@pytest.mark.parametrize("value", [
    {"schema_version": 1, "outputs": ["O1"]},
    {"schema_version": 2, "outputs": "invalid"},
])
def test_invalid_decoder_input_does_not_hide_connected_source(signal_service, value):
    service, metadata, _, calls = signal_service
    service.engine.applied_snapshot["PLUGINS.bindings"][0]["processing_input"] = value
    service.profiles.materialize = lambda _: deepcopy(service.engine.applied_snapshot)

    context = service.read()
    assert context["selected_source_id"] == "source"
    assert context["acquisition_descriptor"] == deepcopy(metadata)
    assert context["processing_input"] is None
    assert context["accepted_input"] is None
    assert context["active_input"] is None
    assert context["source_error"]
    assert not context["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError):
        service.prepare()
    preview = service.preview_input({"outputs": [{"source_channel_id": "channel-0", "name": "Oz"}]})
    assert preview["plan"]["source_indices"] == [0]
    assert not calls


@pytest.mark.parametrize("parameters,error", [
    ({"sample_rate": 1000, "channel_count": 3}, "differs from the experiment profile"),
    ({"sample_rate": 1000}, "differs from the experiment profile"),
])
def test_reported_source_expectations_gate_prepare_without_hiding_actual_info(signal_service, parameters, error):
    service, metadata, _, calls = signal_service
    source = {**metadata, 'provenance': "device-reported"}
    service.acquisition.cached_descriptor = deepcopy(source)
    service.engine.applied_snapshot["PLUGINS.bindings"].append({
        "slot": "acquisition.source", "parameters": parameters,
    })
    assert service.read()["acquisition_descriptor"] == deepcopy(source)
    with pytest.raises(ConfigProfileValidationError, match=error):
        service.prepare()
    assert not calls


@pytest.mark.parametrize("provenance", ["device-reported", "file-reported"])
@pytest.mark.parametrize("parameters", [{}, {"sample_rate": 500}, {"channel_count": 3}])
def test_prepare_uses_observed_metadata_when_expectations_are_optional(signal_service, provenance, parameters):
    service, metadata, _, calls = signal_service
    source = {**metadata, 'provenance': provenance}
    service.acquisition.cached_descriptor = deepcopy(source)
    service.engine.applied_snapshot["PLUGINS.bindings"].append({
        "slot": "acquisition.source", "parameters": parameters,
    })
    service.prepare()
    assert calls[0][1]["plan"]["source"] == descriptor_layout(source).to_dict()
    assert service.read()["readiness"]["ready"]


def test_identity_mapping_prepares_using_observed_signal(signal_service):
    service, metadata, _, calls = signal_service
    service.engine.applied_snapshot["PLUGINS.bindings"][0]["processing_input"] = ProcessingInput().to_dict()
    service.prepare()
    assert calls[0][1]["plan"]["source_indices"] == [0, 1]
    assert calls[0][1]["plan"]["source"]["sample_rate"] == metadata["sample_rate"]


def test_processing_waits_for_acquisition_then_attaches_without_restarting(signal_service):
    service, _, _, calls = signal_service
    service.engine.is_module_running = lambda module: module == "processing"
    assert service.prepare() is None
    assert not calls and not service.read()["readiness"]["ready"]
    service.engine.is_module_running = lambda module: True
    receipt = service.prepare()
    assert receipt["input_id"]
    assert [event for event, _ in calls] == [EventType.PrepareSignalProcessing, EventType.AttachProcessingInput]
    assert service.read()["readiness"]["ready"]


def test_processing_trial_update_preserves_attachment_and_prepared_plan(signal_service):
    service, _, _, calls = signal_service
    original = service.prepare()
    service.engine.applied_snapshot["CONFIG.module_revisions"]["processing"] = "new-processing"
    current = service.prepare()
    assert current["input_id"] == original["input_id"]
    assert current["processing_revision"] == "new-processing"
    assert len(calls) == 2
    assert service.read()["readiness"]["ready"]


def test_attachment_is_not_ready_until_consumer_confirms_and_failure_closes_gate(signal_service):
    service, _, _, calls = signal_service
    request = service.commands.request
    def reject_attach(event, payload):
        result = request(event, payload)
        if event is EventType.AttachProcessingInput:
            assert not service.read()["readiness"]["ready"]
            return SimpleNamespace(data={})
        return result
    service.commands.request = reject_attach
    with pytest.raises(RuntimeError, match="input boundary"):
        service.prepare()
    assert [event for event, _ in calls] == [EventType.PrepareSignalProcessing,
                                          EventType.AttachProcessingInput, EventType.DetachProcessingInput]
    assert service._receipt is None
    assert not service.read()["readiness"]["ready"]


def test_gone_source_resets_old_input_without_claiming_a_drain(signal_service):
    service, _, _, calls = signal_service
    service.prepare()
    service.engine.is_module_running = lambda module: module == "processing"
    assert not service.read()["readiness"]["ready"]
    assert service.prepare() is None
    assert calls[-1][0] is EventType.ResetProcessingInput
    assert service._receipt is None


def test_dead_processor_closes_producer_gate_without_waiting_for_drain(signal_service):
    service, _, _, calls = signal_service
    service.prepare()
    service.engine.is_module_running = lambda module: module == "acquisition"
    result = service.detach()
    assert result.processing_revision is None
    assert calls[-1][0] is EventType.DetachProcessingInput
    assert calls[-1][1]["drain"] is False
    assert service._receipt is None


def test_metadata_drift_detaches_but_is_not_silently_accepted_on_retry(signal_service):
    service, metadata, _, calls = signal_service
    service.prepare()
    service.acquisition.cached_descriptor = {**metadata, 'sample_rate': 1000}
    for _ in range(2):
        with pytest.raises(ConfigProfileValidationError, match="metadata changed"):
            service.prepare()
        assert calls[-1][0] is EventType.DetachProcessingInput
    assert len([event for event, _ in calls if event is EventType.PrepareSignalProcessing]) == 1
    assert not service.read()["readiness"]["ready"]


def test_current_input_fault_is_visible_and_quarantines_only_processing(signal_service):
    service, _, _, _ = signal_service
    receipt = service.prepare()
    fault = {key: receipt[key] for key in ("generation", "engine_revision", "acquisition_revision", "input_id")}
    service.on_input_fault(Event(EventType.ProcessingInputFault, {**fault, "input_id": "old-input", "reason": "old failure"}))
    assert service.read()["readiness"]["ready"]
    service.on_input_fault(Event(EventType.ProcessingInputFault, {**fault, "reason": "Processing input queue is full"}))
    assert "queue is full" in service.read()["source_error"]
    assert not service.read()["readiness"]["ready"]
    assert service.engine.admission.requires_recovery("processing")
    assert not service.engine.admission.requires_recovery("acquisition")


def test_fault_before_attachment_acknowledgement_cannot_publish_ready(signal_service):
    service, _, _, calls = signal_service
    request = service.commands.request
    def fault_during_attach(event, payload):
        result = request(event, payload)
        if event is EventType.AttachProcessingInput:
            service.on_input_fault(Event(EventType.ProcessingInputFault, {**payload, "reason": "Input overflow"}))
        return result
    service.commands.request = fault_during_attach
    with pytest.raises(RuntimeError, match="Input overflow"):
        service.prepare()
    assert service._receipt is None and service._pending_receipt is None
    assert not service.read()["readiness"]["ready"]
    assert calls[-1][0] is EventType.DetachProcessingInput
