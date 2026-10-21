"""Prepare real CNT acquisition and mapping across spawned worker boundaries."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand, DisconnectDataProviderCommand, StartStreamCommand, StopStreamCommand,
)
from omnibci.application.commands.engine_commands import StartEngineCommand, StopEngineCommand
from omnibci.application.services.command_result_service import CommandRejectedError
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.plugin_profile_service import PluginProfileService
from omnibci.application.services.runtime_apply_service import RuntimeApplyCoordinator
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.signal_setup_service import SignalSetupService
from omnibci.bootstrap import assemble, register_engine_listeners, _acquisition_binding
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, ResolvedSignalPlan
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from omnibci.infrastructure.plugins.configuration_catalog import CatalogConfigurationAdapter
from omnibci.infrastructure.repositories.plugin_profile_repository import TomlPluginProfileRepository
from omnibci.shared.events import EventType


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cnt_mapping_prepares_before_streaming_and_rejects_stale_connection(tmp_path):
    processing_input = ProcessingInput.from_dict({
        "schema_version": 2, "outputs": [{"source_channel_id": "channel-8", "name": "O2"},
                                           {"source_channel_id": "channel-0", "name": "Pz"}],
    })
    host = {"NEUROENGINE.message_server_port": 0, "NEUROENGINE.startup_timeout_s": 15.0,
            "NEUROENGINE.command_timeout_s": 30.0, "STIMULATION.auto_spawn": True,
            "ACTUATION.enabled": False, "ALGORITHM.baseline_duration": 0.2}
    profiles = PluginProfileService(TomlPluginProfileRepository(tmp_path / "profiles"),
                                    CatalogConfigurationAdapter(), settings_source=lambda: host)
    selections = []
    for slot, kind, alias, parameters in (
        ("acquisition.source", "acquisition", "file_reader", {
            "data_file_path": str(Path(__file__).parents[1] / "fixture" / "train.cnt"),
            "file_reader_chunk_size": 100,
        }),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
        ("stimulation.paradigm", "paradigm", "STIMULATION_THUBCI_SSVEP_classic40", {}),
    ):
        selection = profiles.catalog.legacy(kind, alias).selection
        if kind == "acquisition":
            selections.append({"slot": slot, "selection": selection.to_dict(), "parameters": parameters})
            continue
        extra = {"processing_input": processing_input} if kind == "decoder" else {}
        profile = profiles.create(kind, selection, alias, parameters, **extra)
        selections.append({"slot": slot, "profile_id": profile.profile_id})
    decoder_a = profiles.repository.get_profile(selections[1]["profile_id"])
    decoder_b = profiles.create("decoder", decoder_a.selection, "Reordered channels", {},
                                processing_input=ProcessingInput(outputs=(ChannelBinding("Pz", "channel-0"), ChannelBinding("O2", "channel-8"))))
    missing_decoder = profiles.create("decoder", decoder_a.selection, "Missing channel", {},
                                      processing_input=ProcessingInput(outputs=(ChannelBinding("NotPresent", "not-present"),)))
    profiles.accept(profiles.propose(selections), expected_revision=None)
    engine = NeuroEngineService(lambda snapshot: NeuroEngineBuilder(settings_snapshot=snapshot).build(),
                                snapshot_provider=profiles.resolve_settings)
    await engine.startup()
    container = assemble(engine, settings=host, admission=engine.admission)
    container.acquisition_service.set_metadata_binding(lambda: _acquisition_binding(engine))
    signal = SignalSetupService(SimpleNamespace(read_saved=lambda: (host, "saved")), profiles, engine,
                                container.acquisition_service, container.command_results)
    container.acquisition_service.set_signal_preparer(signal.prepare)
    register_engine_listeners(container)
    coordinator = RuntimeApplyCoordinator(engine, profiles, container.acquisition_service,
                                          container.stimulation_service,
                                          container.command_results, signal_preparer=signal.prepare)
    runtime = RuntimeConfigurationService(profiles, engine, executor=coordinator)
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
        assert profiles.repository.list_profiles("acquisition") == []
        metadata = await asyncio.to_thread(container.acquisition_service.get_metadata)
        assert isinstance(metadata["metadata"], str)
        assert "O2" in metadata["metadata"]
        assert "signal_channel_count" not in metadata["metadata"]
        assert metadata["descriptor"]["output_row_count"] == 10
        assert metadata["descriptor"]["signal_channel_count"] == 9
        assert metadata["descriptor"]["provenance"] == "file-reported"
        assert metadata["descriptor"]["channels"][8]["channel_id"] == "channel-8"
        assert metadata["comparison"]["status"] == "unconfirmed"
        assert profiles.repository.list_profiles("acquisition") == []
        await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
        observed_plan = signal._plan.to_dict()
        assert observed_plan["source"]["sample_rate"] == metadata["descriptor"]["sample_rate"]
        assert observed_plan["source"]["output_row_count"] == metadata["descriptor"]["output_row_count"]
        await asyncio.to_thread(container.acquisition_service.handle_stop_stream, StopStreamCommand())
        assert profiles.repository.list_profiles("acquisition") == []

        # Optionally pin observed dimensions as expectations for subsequent reads.
        source = profiles.repository.get_activation().bindings[0]
        assert source.profile_id is None
        saved_source = profiles.create("acquisition", source.selection, "Observed CNT experiment", {
            **source.parameters, "sample_rate": metadata["descriptor"]["sample_rate"],
            "channel_number": metadata["descriptor"]["output_row_count"],
        })
        assert profiles.repository.get_activation().bindings[0].profile_id is None
        selections[0] = {"slot": "acquisition.source", "profile_id": saved_source.profile_id}
        reviewed = runtime.plan(selections, expected_revision=profiles.repository.get_activation().revision,
                                refresh_slots=["acquisition.source"])
        operation = runtime.submit(reviewed.plan_id, str(uuid4()))
        await runtime.wait_for_idle()
        result = runtime.operation(operation["operation_id"])
        assert result["status"] == "succeeded", result
        assert profiles.repository.get_activation().bindings[0].profile_id == saved_source.profile_id
        metadata = await asyncio.to_thread(container.acquisition_service.get_metadata)
        assert metadata["comparison"]["status"] == "matched"
        assert profiles.repository.get_profile(saved_source.profile_id) == saved_source

        await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
        readiness = signal.read()["readiness"]
        assert readiness["ready"] is True
        original_receipt = readiness["receipt"]
        plan = signal._plan.to_dict()
        assert plan["processing_input"] == processing_input.to_dict()
        assert "setup" not in plan
        assert [channel["name"] for channel in plan["output_channels"]] == ["O2", "Pz"]
        assert all(channel.get("reference") is None for channel in plan["output_channels"])
        assert plan["source"]["connection_id"] == metadata["connection_id"]
        assert plan["source_indices"] == [8, 0]
        raw = np.arange(30, dtype=float).reshape(10, 3)
        raw[-1] = [0, 1, 255]
        chunk = SignalChunk(timestamp=1.0, sequence_id=7, raw_data=raw)
        mapped = ResolvedSignalPlan.from_dict(plan).apply(chunk)
        np.testing.assert_array_equal(mapped.data[0], raw[8])
        np.testing.assert_array_equal(mapped.triggers, chunk.triggers)
        assert mapped.timestamp == chunk.timestamp and mapped.sequence_id == chunk.sequence_id
        await asyncio.sleep(0.05)
        await asyncio.to_thread(container.acquisition_service.handle_stop_stream, StopStreamCommand())

        # Disconnect clears API readiness, and the worker rejects a receipt from that connection.
        await asyncio.to_thread(container.acquisition_service.handle_disconnect, DisconnectDataProviderCommand())
        assert signal.read()["readiness"]["ready"] is False
        assert profiles.repository.get_profile(saved_source.profile_id) == saved_source
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        reconnected = await asyncio.to_thread(container.acquisition_service.get_metadata)
        assert reconnected["connection_id"] != metadata["connection_id"]
        with pytest.raises(CommandRejectedError) as stale:
            await asyncio.to_thread(container.command_results.request, EventType.StartStream, original_receipt)
        assert stale.value.result.error_code == "SIGNAL_NOT_PREPARED"
        await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
        assert signal.read()["readiness"]["receipt"]["connection_id"] == reconnected["connection_id"]
        await asyncio.to_thread(container.acquisition_service.handle_stop_stream, StopStreamCommand())
        assert plan["source"]["connection_id"] == metadata["connection_id"]

        # Restart reads each decoder's persisted input instead of a shared global setup.
        for decoder, outputs in (
            (decoder_b, ["Pz", "O2"]), (decoder_a, ["O2", "Pz"]),
        ):
            await engine.handle_stop_engine(StopEngineCommand())
            profiles.repository = TomlPluginProfileRepository(tmp_path / "profiles")
            assert profiles.repository.get_profile(decoder.profile_id).processing_input == decoder.processing_input
            selections[1] = {"slot": "processing.decoder", "profile_id": decoder.profile_id}
            previous = profiles.repository.get_activation()
            profiles.accept(profiles.propose(selections), expected_revision=previous.revision)
            await engine.handle_start_engine(StartEngineCommand())
            await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
            await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
            current_plan = signal._plan.to_dict()
            assert [channel["name"] for channel in current_plan["output_channels"]] == outputs
            assert all(channel.get("reference") is None for channel in current_plan["output_channels"])
            transformed = ResolvedSignalPlan.from_dict(current_plan).apply(chunk)
            np.testing.assert_array_equal(transformed.triggers, chunk.triggers)
            await asyncio.to_thread(container.acquisition_service.handle_stop_stream, StopStreamCommand())

        await engine.handle_stop_engine(StopEngineCommand())
        selections[1] = {"slot": "processing.decoder", "profile_id": missing_decoder.profile_id}
        previous = profiles.repository.get_activation()
        profiles.accept(profiles.propose(selections), expected_revision=previous.revision)
        await engine.handle_start_engine(StartEngineCommand())
        await asyncio.to_thread(container.acquisition_service.handle_connect, ConnectDataProviderCommand())
        with pytest.raises(ValueError, match="Missing acquisition channel: not-present"):
            await asyncio.to_thread(container.acquisition_service.handle_start_stream, StartStreamCommand())
        assert container.acquisition_service.is_streaming is False
        assert signal.read()["readiness"]["ready"] is False
    finally:
        await engine.handle_stop_engine(StopEngineCommand())
