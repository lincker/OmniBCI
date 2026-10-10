"""Signal setup remains a draft until review, and preparation precedes sessions."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from threading import RLock
from types import SimpleNamespace

import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand
from omnibci.application.config_errors import ConfigProfileConflictError, ConfigProfileValidationError
from omnibci.application.plugin_profiles import ActivationSnapshot
from omnibci.application.services.session_service import SessionAppService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.conf import OmniBCISettings
from omnibci.domain.montage import ChannelDescriptor, SourceMetadata
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.shared.events import EventType


@pytest.fixture
def signal_service(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text('[NEUROENGINE]\nqueue_size = 100\n', encoding="utf-8")
    settings = OmniBCISettings(path)
    metadata = SourceMetadata("source", 500, (
        ChannelDescriptor(0, "O1", unit="uV"), ChannelDescriptor(1, "M1", unit="uV"),
    ), 2, connection_id="connection", original_reference="Cz")
    setup = {"source_id": "source", "outputs": ["O1"],
             "reference": {"mode": "single", "channels": ["M1"]}}
    snapshot = {"SIGNAL.setup": setup, "CONFIG.revision": "a" * 64,
                "CONFIG.generation": "generation", "ACQUISITION.sample_rate": 500,
                "ACQUISITION.signal_source": "source", "PLUGINS.bindings": []}
    engine = SimpleNamespace(admission=None, is_engine_ready=True, applied_snapshot=snapshot,
                             configuration_change=lambda idle: nullcontext())
    profiles = SimpleNamespace(repository=SimpleNamespace(get_activation=lambda: ActivationSnapshot({}, ())),
                               materialize=lambda activation: {})
    acquisition = SimpleNamespace(lifecycle_lock=RLock(), cached_metadata=metadata.to_dict(),
                                  handle_get_status=lambda query: SimpleNamespace(streaming=False))
    acquisition.get_metadata = lambda **kwargs: deepcopy(acquisition.cached_metadata)
    calls = []

    def request(event, payload):
        calls.append((event, deepcopy(payload)))
        return SimpleNamespace(data=deepcopy(payload["receipt"]))

    layouts = SimpleNamespace(names=lambda: ["template"], get=lambda name: {
        "positions": {"O1": [0.0, -0.08, 0.04]}, "provider": "test template 1",
    })
    service = SignalSetupService(settings, profiles, engine, acquisition,
                                SimpleNamespace(request=request), layouts)
    return service, metadata, setup, calls


def test_saved_nested_setup_roundtrips_without_applying(signal_service):
    service, metadata, setup, calls = signal_service
    setup = {**setup, "manual_metadata": metadata.to_dict(), "template": "template"}
    # Existing declarations remain readable; new ones belong to acquisition profiles.
    _, initial_revision = service.settings.read_saved()
    service.settings.update({"SIGNAL.setup": setup}, expected_revision=initial_revision)
    before = deepcopy(service.engine.applied_snapshot)
    revision = service.read()["revision"]
    result = service.save(setup, expected_revision=revision)
    fresh = OmniBCISettings(service.settings._config_path)
    assert fresh.read_saved()[0]["SIGNAL.setup"] == result["setup"]
    assert service.engine.applied_snapshot == before
    assert not calls
    with pytest.raises(ConfigProfileConflictError):
        service.save(setup, expected_revision=revision)


def test_new_manual_source_declarations_belong_to_acquisition_profiles(signal_service):
    service, metadata, setup, _ = signal_service
    before = service.settings.read_saved()
    with pytest.raises(ConfigProfileValidationError, match="acquisition profile"):
        service.save({**setup, "manual_metadata": metadata.to_dict()}, expected_revision=before[1])
    assert service.settings.read_saved() == before


def test_disconnected_mapping_preview_uses_pinned_acquisition_profile_montage(signal_service):
    from uuid import uuid4
    from omnibci.application.plugin_profiles import PluginProfile, PluginSelection, ProfileBinding
    service, metadata, setup, _ = signal_service
    selection = PluginSelection("test.source", "default", "1")
    profile = PluginProfile(str(uuid4()), "Source", "acquisition", selection,
        {"sample_rate": 500, "channel_number": 3}, revision=1,
        acquisition_montage=replace(metadata, original_reference=None), manual_montage=metadata)
    binding = ProfileBinding("acquisition.source", "acquisition", profile.profile_id,
        selection, profile.parameters, profile_revision=1, manual_montage=profile.manual_montage)
    service.engine.applied_snapshot["PLUGINS.bindings"] = [binding.to_dict()]
    service.acquisition.cached_metadata = None
    service.profiles.repository.get_profile = lambda profile_id: profile
    description = service.read()["source_metadata"]
    assert description["original_reference"] == "Cz"
    assert description["connection_id"] == ""
    assert not service.read()["readiness"]["ready"]
    assert service.preview(setup)["plan"]["output_channels"][0]["reference"] == "M1"
    # A saved manual edit has no effect until the source profile is applied.
    profile = replace(profile, manual_montage=replace(metadata, original_reference="M2"))
    assert service.read()["source_metadata"]["original_reference"] == "Cz"


def test_preview_retains_reference_dependencies_and_template_provenance(signal_service):
    service, metadata, setup, calls = signal_service
    result = service.preview({**setup, "template": "template"}, metadata.to_dict())
    plan = result["plan"]
    assert [channel["name"] for channel in plan["output_channels"]] == ["O1"]
    assert plan["coefficients"] == [[1e-6, -1e-6]]
    assert plan["output_channels"][0]["position_provenance"] == "test template 1"
    assert "position" not in metadata.channels[0].to_dict()
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
    service.acquisition.cached_metadata = None
    assert not service.read()["readiness"]["ready"]
    reconnected = replace(metadata, connection_id="new-connection")
    service.acquisition.cached_metadata = reconnected.to_dict()
    assert not service.read()["readiness"]["ready"]
    assert service.prepare()["connection_id"] == "new-connection"
    service.acquisition.cached_metadata = replace(
        reconnected, channels=(replace(metadata.channels[0], name="O2"), metadata.channels[1]),
    ).to_dict()
    assert not service.read()["readiness"]["ready"]
    with pytest.raises(ConfigProfileValidationError, match="metadata changed"):
        service.prepare()
    assert len(calls) == 2


def test_known_rate_conflict_blocks_before_processor_command(signal_service):
    service, metadata, _, calls = signal_service
    service.acquisition.cached_metadata = replace(metadata, sample_rate=1000).to_dict()
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
    assert frozen["output_channels"][0]["reference"] == "M1"
    service.engine.applied_snapshot["SIGNAL.setup"]["outputs"] = ["M1"]
    assert repo.find_by_id(session_id).signal_plan == frozen
    service.commands.request = lambda event, payload: SimpleNamespace(data={})
    with pytest.raises(RuntimeError, match="acknowledge"):
        sessions.handle_create_session(CreateSessionCommand())
    assert len(repo.find_all()) == 1


def test_legacy_configuration_requires_no_metadata_or_preparation(signal_service):
    service, _, _, calls = signal_service
    del service.engine.applied_snapshot["SIGNAL.setup"]
    service.acquisition.get_metadata = lambda **kwargs: pytest.fail("Legacy metadata read")
    assert service.prepare_session() is None
    assert not calls


def test_invalid_preview_does_not_write_or_prepare(signal_service):
    service, metadata, setup, calls = signal_service
    with pytest.raises(ConfigProfileValidationError, match="Missing signal dependency"):
        service.preview({**setup, "outputs": ["missing"]}, metadata.to_dict())
    assert service.read()["setup"] is None
    assert not calls
