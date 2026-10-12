"""Prepare real CNT acquisition and mapping across spawned worker boundaries."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand, DisconnectDataProviderCommand,
)
from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.services.command_result_service import CommandRejectedError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.bootstrap import assemble, register_engine_listeners, _acquisition_montage_binding
from omnibci.domain.montage import ChannelDescriptor, ProcessingInput, ResolvedSignalPlan, SourceMetadata
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.infrastructure.signal_layouts import SignalLayoutProvider
from omnibci.shared.events import EventType


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cnt_mapping_prepares_before_recording_and_rejects_stale_connection(tmp_path):
    processing_input = ProcessingInput.from_dict({
        "outputs": ["O2", "Pz"],
        "reference": {"mode": "single", "channels": ["Oz"]},
    })
    manual = SourceMetadata("file_reader", 1000, tuple(
        ChannelDescriptor(index, f"CH{index + 1}", signal_type="unknown", name_provenance="generated")
        for index in range(9)), 9, original_reference="Cz")
    host = {"NEUROENGINE.message_server_port": 0, "NEUROENGINE.startup_timeout_s": 15.0,
            "NEUROENGINE.command_timeout_s": 30.0, "STIMULATION.auto_spawn": True,
            "ACTUATION.enabled": False, "ALGORITHM.baseline_duration": 0.2}
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "profiles"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: host)
    selections = []
    for slot, kind, alias, parameters in (
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": str(Path(__file__).parents[1] / "fixture" / "train.cnt"),
            "file_reader_chunk_size": 100, "sample_rate": 1000, "channel_number": 10,
        }),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
    ):
        extra = {"manual_montage": manual} if kind == "acquisition" else (
            {"processing_input": processing_input} if kind == "decoder" else {})
        profile = profiles.create(kind, profiles.catalog.legacy(kind, alias).selection, alias, parameters, **extra)
        selections.append({"slot": slot, "profile_id": profile.profile_id})
    decoder_a = profiles.repository.get_profile(selections[1]["profile_id"])
    decoder_b = profiles.create("decoder", decoder_a.selection, "Original reference", {},
                                processing_input=ProcessingInput(outputs=("Pz", "O2")))
    missing_decoder = profiles.create("decoder", decoder_a.selection, "Missing electrode", {},
                                      processing_input=ProcessingInput(outputs=("NotPresent",)))
    profiles.accept(profiles.propose(selections), expected_revision=None)
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build(),
                                snapshot_provider=profiles.resolve_settings)
    await engine.startup()
    container = assemble(engine, InMemorySessionRepository(), settings=host, admission=engine.admission)
    container.acquisition_service.set_metadata_recorder(
        lambda profile_id: _acquisition_montage_binding(engine, profiles, profile_id),
        profiles.record_acquisition_montage,
    )
    signal = SignalSetupService(SimpleNamespace(read_saved=lambda: (host, "saved")), profiles, engine,
                                container.acquisition_service, container.command_results, SignalLayoutProvider())
    container.acquisition_service.set_signal_preparer(signal.prepare)
    container.session_service.set_signal_preparer(signal.prepare_session, container.acquisition_service.lifecycle_lock)
    register_engine_listeners(container)
    try:
        await engine.handle_start_engine(StartEngineCommand())
        assert engine.is_engine_ready
        assert "SIGNAL.setup" not in engine.applied_snapshot
        assert signal.read()["readiness"]["ready"] is False
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())

        # A process-ready engine still rejects samples before the plan is acknowledged.
        with pytest.raises(CommandRejectedError) as unprepared:
            await asyncio.to_thread(container.command_results.request, EventType.StartStream)
        assert unprepared.value.result.error_code == "SIGNAL_NOT_PREPARED"
        metadata = await asyncio.to_thread(container.acquisition_service.get_metadata)
        assert metadata["output_row_count"] == 10
        assert metadata["trigger_row_index"] == 9
        assert metadata["channels"][8]["name"] == "O2"
        saved_source = TomlPluginProfileRepository(tmp_path / "profiles").get_profile(selections[0]["profile_id"])
        assert saved_source.acquisition_montage.connection_id == ""
        assert saved_source.acquisition_montage.channels[8].name == "O2"
        assert saved_source.acquisition_montage.original_reference is None
        assert saved_source.acquisition_montage.provenance == "file-reported"

        session_id = await asyncio.to_thread(container.recording_service.start)
        readiness = signal.read()["readiness"]
        assert readiness["ready"] is True
        original_receipt = readiness["receipt"]
        session = container.session_repository.find_by_id(session_id)
        plan = session.signal_plan
        assert plan["processing_input"] == processing_input.to_dict()
        assert "setup" not in plan
        assert [channel["name"] for channel in plan["output_channels"]] == ["O2", "Pz"]
        assert [channel["reference"] for channel in plan["output_channels"]] == ["Oz", "Oz"]
        assert plan["source"]["connection_id"] == metadata["connection_id"]
        assert plan["coefficients"][0] == [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0, 1.0]
        raw = np.arange(30, dtype=float).reshape(10, 3)
        raw[-1] = [0, 1, 255]
        chunk = SignalChunk(timestamp=1.0, sequence_id=7, raw_data=raw)
        mapped = ResolvedSignalPlan.from_dict(plan).apply(chunk)
        np.testing.assert_array_equal(mapped.data[0], raw[8] - raw[7])
        np.testing.assert_array_equal(mapped.triggers, chunk.triggers)
        assert mapped.timestamp == chunk.timestamp and mapped.sequence_id == chunk.sequence_id
        await asyncio.sleep(0.05)
        assert await asyncio.to_thread(container.recording_service.stop) == session_id

        # Disconnect clears API readiness, and the worker rejects a receipt from that connection.
        await asyncio.to_thread(container.acquisition_service.handle_disconnect, DisconnectDataProviderCommand())
        assert signal.read()["readiness"]["ready"] is False
        assert profiles.repository.get_profile(saved_source.profile_id).acquisition_montage == saved_source.acquisition_montage
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        reconnected = await asyncio.to_thread(container.acquisition_service.get_metadata)
        assert reconnected["connection_id"] != metadata["connection_id"]
        with pytest.raises(CommandRejectedError) as stale:
            await asyncio.to_thread(container.command_results.request, EventType.StartStream, original_receipt)
        assert stale.value.result.error_code == "SIGNAL_NOT_PREPARED"
        next_session = await asyncio.to_thread(container.recording_service.start)
        assert next_session != session_id
        assert signal.read()["readiness"]["receipt"]["connection_id"] == reconnected["connection_id"]
        assert await asyncio.to_thread(container.recording_service.stop) == next_session
        assert session.signal_plan == plan

        # Restart reads each decoder's persisted input instead of a shared global setup.
        for decoder, outputs, reference in (
            (decoder_b, ["Pz", "O2"], "Cz"), (decoder_a, ["O2", "Pz"], "Oz"),
        ):
            await engine.handle_stop_engine(StopEngineCommand())
            profiles.repository = TomlPluginProfileRepository(tmp_path / "profiles")
            assert profiles.repository.get_profile(decoder.profile_id).processing_input == decoder.processing_input
            selections[1] = {"slot": "processing.decoder", "profile_id": decoder.profile_id}
            previous = profiles.repository.get_activation()
            profiles.accept(profiles.propose(selections), expected_revision=previous.revision)
            await engine.handle_start_engine(StartEngineCommand())
            await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
            current_session = await asyncio.to_thread(container.recording_service.start)
            current_plan = container.session_repository.find_by_id(current_session).signal_plan
            assert [channel["name"] for channel in current_plan["output_channels"]] == outputs
            assert [channel["reference"] for channel in current_plan["output_channels"]] == [reference, reference]
            transformed = ResolvedSignalPlan.from_dict(current_plan).apply(chunk)
            np.testing.assert_array_equal(transformed.triggers, chunk.triggers)
            assert await asyncio.to_thread(container.recording_service.stop) == current_session

        await engine.handle_stop_engine(StopEngineCommand())
        selections[1] = {"slot": "processing.decoder", "profile_id": missing_decoder.profile_id}
        previous = profiles.repository.get_activation()
        profiles.accept(profiles.propose(selections), expected_revision=previous.revision)
        await engine.handle_start_engine(StartEngineCommand())
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        with pytest.raises(ValueError, match="Missing signal dependency: NotPresent"):
            await asyncio.to_thread(container.recording_service.start)
        assert container.session_service.active_session_id is None
        assert signal.read()["readiness"]["ready"] is False
    finally:
        await engine.handle_stop_engine(StopEngineCommand())
