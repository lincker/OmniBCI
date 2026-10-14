"""Signal setup remains a draft until review, and preparation precedes sessions."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand
from omnibci.application.config_errors import ConfigProfileValidationError
from omnibci.application.plugin_profiles import ActivationSnapshot
from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.conf import OmniBCISettings
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.shared.events import EventType


@pytest.fixture
def signal_service(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\nqueue_size = 100\n', encoding="utf-8")
    settings = OmniBCISettings(path)
    metadata = SignalInfo("source", 500, (
        ChannelDescriptor(0, "O1", unit="uV"), ChannelDescriptor(1, "M1", unit="uV"),
    ), 2, connection_id="connection")
    setup = ProcessingInput(outputs=(ChannelBinding("O1", "channel-0"),)).to_dict()
    snapshot = { "CONFIG.revision": "a" * 64,
                "CONFIG.generation": "generation", "ACQUISITION.sample_rate": 500,
                "ACQUISITION.signal_source": "source", "PLUGINS.bindings": [
                    {"slot": "processing.decoder", "processing_input": setup,
                     "selection": {"package_id": "test.decoder", "contribution_id": "default", "version": "1"}}]}
    engine = SimpleNamespace(admission=None, is_engine_ready=True, applied_snapshot=snapshot,
                             configuration_change=lambda idle: nullcontext())
    profiles = SimpleNamespace(repository=SimpleNamespace(get_activation=lambda: ActivationSnapshot({}, ())),
                               materialize=lambda activation: {},
                               catalog=SimpleNamespace(get=lambda *args: SimpleNamespace(channel_requirements=None)))
    acquisition = SimpleNamespace(lifecycle_lock=RLock(), cached_descriptor=metadata.to_dict(),
                                  handle_get_status=lambda query: SimpleNamespace(streaming=False))
    acquisition.cached_descriptor_for = lambda binding: deepcopy(acquisition.cached_descriptor)
    acquisition.get_descriptor = lambda **kwargs: deepcopy(acquisition.cached_descriptor)
    calls = []

    def request(event, payload):
        calls.append((event, deepcopy(payload)))
        return SimpleNamespace(data=deepcopy(payload["receipt"]))

    service = SignalSetupService(settings, profiles, engine, acquisition,
                                SimpleNamespace(request=request))
    return service, metadata, setup, calls






def test_disconnected_mapping_preview_requires_metadata_from_the_current_connection(signal_service):
    service, _, _, calls = signal_service
    service.acquisition.cached_descriptor = None
    assert service.read()["acquisition_descriptor"] is None
    assert not service.read()["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError, match="Connect the acquisition source and get metadata"):
        service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]})
    assert not calls




def test_preparation_uses_current_observation_and_checks_acknowledgement(signal_service):
    service, metadata, setup, calls = signal_service
    receipt = service.prepare()
    assert calls[0][0] is EventType.PrepareSignalProcessing
    assert receipt["connection_id"] == metadata.connection_id
    assert receipt["source_fingerprint"] == metadata.fingerprint
    assert service.read()["readiness"]["ready"]
    service.commands.request = lambda event, payload: SimpleNamespace(data={})
    with pytest.raises(RuntimeError, match="acknowledge"):
        service.prepare()


def test_reconnect_requires_new_receipt_and_source_drift_blocks_prepare(signal_service):
    service, metadata, _, calls = signal_service
    service.prepare()
    service.acquisition.cached_descriptor = None
    assert not service.read()["readiness"]["ready"]
    reconnected = replace(metadata, connection_id="new-connection")
    service.acquisition.cached_descriptor = reconnected.to_dict()
    assert not service.read()["readiness"]["ready"]
    assert service.prepare()["connection_id"] == "new-connection"
    service.acquisition.cached_descriptor = replace(
        reconnected, channels=(replace(metadata.channels[0], name="O2"), metadata.channels[1]),
    ).to_dict()
    assert not service.read()["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError, match="metadata changed"):
        service.prepare()
    assert len(calls) == 2


def test_known_rate_conflict_blocks_before_processor_command(signal_service):
    service, metadata, _, calls = signal_service
    service.acquisition.cached_descriptor = replace(metadata, sample_rate=1000).to_dict()
    with pytest.raises(ConfigProfileValidationError, match="sample rate"):
        service.prepare()
    assert not calls


def test_session_freezes_plan_and_failure_creates_no_record(signal_service):
    service, _, _, _ = signal_service
    repo = InMemorySessionRepository()
    sessions = SessionAppService(repo)
    sessions.set_signal_preparer(service.prepare_session, service.acquisition.lifecycle_lock)
    session_id = sessions.handle_create_session(CreateSessionCommand())
    frozen = repo.find_by_id(session_id).signal_plan
    assert frozen["source_indices"] == [0]
    service.engine.applied_snapshot["PLUGINS.bindings"][0]["processing_input"]["outputs"] = [{"source_channel_id": "channel-1", "name": "M1"}]
    assert repo.find_by_id(session_id).signal_plan == frozen
    service.commands.request = lambda event, payload: SimpleNamespace(data={})
    with pytest.raises(RuntimeError, match="acknowledge"):
        sessions.handle_create_session(CreateSessionCommand())
    assert len(repo.find_all()) == 1


def test_legacy_configuration_requires_no_metadata_or_preparation(signal_service):
    service, _, _, calls = signal_service
    service.engine.applied_snapshot["PLUGINS.bindings"].clear()
    service.acquisition.get_descriptor = lambda **kwargs: pytest.fail("Legacy metadata read")
    assert service.prepare_session() is None
    assert not calls


def test_invalid_preview_does_not_write_or_prepare(signal_service):
    service, metadata, setup, calls = signal_service
    with pytest.raises(ConfigProfileValidationError, match="Missing acquisition channel"):
        service.preview_input({"outputs": [{'source_channel_id': 'missing', 'name': 'missing'}]})
    assert "setup" not in service.read()
    assert not calls


def test_processing_input_preview_uses_source_facts_without_writing(signal_service):
    service, metadata, _, calls = signal_service
    before = service.settings.read_saved()
    result = service.preview_input({"outputs": [{'source_channel_id': 'channel-0', 'name': 'O1'}]})
    assert result["plan"]["source_indices"] == [0]
    assert result["plan"]["source"] == metadata.to_dict()
    assert service.settings.read_saved() == before
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
    from omnibci.application.plugin_profiles import PluginProfile, PluginSelection, ProfileBinding
    from omnibci.domain.channel_mapping import ProcessingInput
    service, _, _, _ = signal_service
    selection = PluginSelection("test.decoder", "default", "1")
    profile = PluginProfile(str(uuid4()), "Decoder", "decoder", selection, {}, revision=1,
                            processing_input=ProcessingInput(outputs=(ChannelBinding('O1', 'channel-0'),)))
    binding = ProfileBinding("processing.decoder", "decoder", profile.profile_id, selection,
                             {}, profile_revision=1, processing_input=profile.processing_input)
    accepted = ActivationSnapshot({}, (binding,))
    service.profiles.repository.get_activation = lambda: accepted
    service.profiles.repository.get_profile = lambda profile_id: profile
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
              "parameters": {"sample_rate": 500, "channel_number": 3}}
    snapshot = {**service.engine.applied_snapshot,
                "PLUGINS.bindings": [source, {"slot": "processing.decoder",
                    "selection": {"package_id": "test.decoder", "contribution_id": "default", "version": "1"},
                    "processing_input": ProcessingInput(outputs=tuple(ChannelBinding(name, {"O1": "channel-0", "M1": "channel-1"}[name]) for name in outputs)).to_dict()}]}
    snapshot.pop("SIGNAL.setup", None)
    service.profiles.repository.get_profile = lambda profile_id: None
    service.engine.applied_snapshot = deepcopy(snapshot)
    service.profiles.materialize = lambda activation: deepcopy(snapshot)
    return snapshot


def test_preview_uses_edited_decoder_requirements_without_changing_selected_input(signal_service):
    from omnibci.domain.decoder_input import DecoderChannelRequirements
    service, metadata, _, calls = signal_service
    snapshot = modern_binding(service, metadata)
    before = deepcopy(snapshot)
    edited = {"package_id": "test.edited", "contribution_id": "default", "version": "1"}
    service.profiles.catalog.get = lambda kind, selection: SimpleNamespace(
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
    service.profiles.catalog.get = lambda *args: SimpleNamespace(
        channel_requirements=DecoderChannelRequirements(required_names=("M1",)))
    with pytest.raises(ConfigProfileValidationError, match="M1"):
        service.prepare()
    assert not calls


def test_modern_preparation_and_session_use_native_input_without_global_setup(signal_service):
    service, metadata, _, calls = signal_service
    snapshot = modern_binding(service, metadata)
    plan = service.prepare_session()
    assert "SIGNAL.setup" not in snapshot
    assert "setup" not in plan
    assert plan["processing_input"]["outputs"] == [{"source_channel_id": "channel-0", "name": "O1"}]
    assert plan["source"] == metadata.to_dict()
    assert service.read()["readiness"]["ready"]
    assert len(calls) == 1


def test_source_switch_waits_for_connected_metadata_and_recompiles_mapping(signal_service):
    from omnibci.application.processing_source import acquisition_binding_key
    service, metadata, _, calls = signal_service
    selected = modern_binding(service, metadata)
    owner = acquisition_binding_key(selected["PLUGINS.bindings"][0])
    service.acquisition.cached_descriptor_for = lambda binding: (
        deepcopy(service.acquisition.cached_descriptor) if acquisition_binding_key(binding) == owner else None)
    service.prepare()
    before = service.read()
    reordered = replace(metadata, connection_id="new", channels=(
        replace(metadata.channels[1], index=0), replace(metadata.channels[0], index=1)))
    selected["PLUGINS.bindings"][0]["profile_id"] = "source-b"
    selected["CONFIG.revision"] = "b" * 64
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
    assert calls[-1][1]["plan"]["source_indices"] == [0]
    service.engine.applied_snapshot = deepcopy(selected)
    service.acquisition.cached_descriptor = reordered.to_dict()
    owner = acquisition_binding_key(selected["PLUGINS.bindings"][0])
    service.prepare()
    assert calls[-1][1]["plan"]["source_indices"] == [1]
    assert service.read()["readiness"]["ready"]


def test_stale_preview_revision_rejected_after_reconnect(signal_service):
    from omnibci.application.plugin_profiles import ProfileRevisionConflict
    service, metadata, _, calls = signal_service
    modern_binding(service, metadata)
    revision = service.read()["source_revision"]
    service.acquisition.cached_descriptor = replace(metadata, connection_id="new-connection").to_dict()
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
    service.acquisition.cached_descriptor = replace(metadata, source_id="other-device").to_dict()
    with pytest.raises(ConfigProfileValidationError, match="different acquisition source"):
        service.prepare()
    assert not calls


def test_saved_profile_metadata_cannot_supply_disconnected_source_facts(signal_service):
    service, metadata, _, _ = signal_service
    snapshot = modern_binding(service, metadata)
    service.acquisition.cached_descriptor = None
    service.profiles.repository.get_profile = lambda profile_id: SimpleNamespace(
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
    changed = replace(metadata, connection_id="next-connection", channels=(
        replace(metadata.channels[0], name="O2"), metadata.channels[1]))
    service.acquisition.cached_descriptor = changed.to_dict()
    with pytest.raises(ConfigProfileValidationError, match="metadata changed"):
        service.prepare()
    selected["PLUGINS.bindings"][0]["profile_revision"] += 1
    selected["CONFIG.generation"] = "rebuilt"
    service.engine.applied_snapshot = deepcopy(selected)
    service.prepare()
    assert calls[-1][1]["plan"]["output_channels"][0]["name"] == "O2"
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


def test_legacy_selected_input_remains_readable_and_can_preview_a_replacement(signal_service):
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    service, metadata, _, calls = signal_service
    source = ProfileBinding("acquisition.source", "acquisition", None,
                            PluginSelection("test.source", "default", "1"), {})
    decoder = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {},
                             legacy={"processing_input": {"schema_version": 1,
                                 "reference": {"mode": "single", "channels": ["M1"]}}})
    accepted = ActivationSnapshot({}, (source, decoder))
    service.profiles.repository.get_activation = lambda: accepted
    service.profiles.materialize = lambda _: (_ for _ in ()).throw(ValueError("Reconfigure legacy mapping"))
    service.profiles.catalog.get = lambda *args: SimpleNamespace(plugin_type="source", channel_requirements=None)

    context = service.read()
    assert context["selected_source_id"] == "source"
    assert context["processing_input"] is None
    assert context["accepted_input"] is None
    assert context["source_error"] == "Reconfigure legacy mapping"
    assert context["acquisition_descriptor"] == metadata.to_dict()
    assert not context["readiness"]["ready"]
    preview = service.preview_input({"outputs": [{"source_channel_id": "channel-0", "name": "Oz"}]})
    assert preview["plan"]["source_indices"] == [0]
    assert not calls
