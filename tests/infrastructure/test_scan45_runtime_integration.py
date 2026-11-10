"""Scan45 events survive numeric input aliases and reach direct web results."""

import asyncio
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from omnibci.application.events import CommandRequest, TrialOutcome
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.application.services.results_service import ResultsService
from omnibci.domain.channel_mapping import ChannelBinding, ProcessingInput, compile_processing_plan
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.outbound import event_to_wire
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import (
    Scan45DataReader, Scan45Message,
)
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.presentation.schemas.ws import TrialCompletedMessage
from omnibci.shared.events import EventType


@pytest.mark.integration
@pytest.mark.asyncio
async def test_scan45_ch1_to_ch9_numeric_mapping_publishes_trial_without_session_commands(monkeypatch):
    sample_rate = 500
    generation, revision = "scan45-generation", "scan45-revision"
    reader = Scan45DataReader(parameters={"channel_count": 10, "sample_rate": sample_rate})
    source = reader.describe_output(source_id=reader.plugin_type, connection_id="scan45-connection")
    assert [channel.name for channel in source.channels] == [f"CH{index}" for index in range(1, 10)]
    assert source.output_row_count == 10 and source.trigger_row_index == 9
    inputs = ProcessingInput(outputs=tuple(
        ChannelBinding(str(index + 1), channel.channel_id, "eeg")
        for index, channel in enumerate(source.channels)
    ))
    plan = compile_processing_plan(source, inputs, config_revision=revision)
    assert [channel.name for channel in plan.output_channels] == [str(index) for index in range(1, 10)]
    assert all(channel.signal_type == "eeg" for channel in plan.output_channels)

    catalog = PluginCatalog()
    snapshot = {
        "CONFIG.generation": generation, "CONFIG.revision": revision,
        "ACQUISITION.signal_source": reader.plugin_type,
        "ACQUISITION.sample_rate": sample_rate, "ACQUISITION.channel_count": 10,
        "PLUGINS.bindings": [
            ProfileBinding(
                "processing.decoder", "decoder", None,
                catalog.resolve("decoder", "THUBCI_SSVEP_classic40CCA").selection,
                {"window_duration": 1.0, "offset_time": 0}, processing_input=inputs,
            ).to_dict(),
            ProfileBinding(
                "processing.epoch_detector", "epoch_detector", None,
                catalog.resolve("epoch_detector", "DEFAULT_EPOCH_DETECTOR").selection, {},
            ).to_dict(),
        ],
    }
    receipt = {
        "generation": generation, "config_revision": revision,
        "connection_id": source.connection_id,
        "source_fingerprint": source.fingerprint, "plan_fingerprint": plan.fingerprint,
    }
    bus = EventBus(Queue())
    data = DataManager(Queue())
    acquired = []
    put_data = data.put_data

    def capture(chunk):
        acquired.append(chunk)
        put_data(chunk)

    monkeypatch.setattr(data, "put_data", capture)
    results = ResultsService(SimpleNamespace(generation=generation))
    prepared = asyncio.get_running_loop().create_future()
    received = asyncio.Event()
    outcomes, messages = [], []

    def on_command(event):
        if event.data.command_id == "prepare":
            prepared.set_result(event.data)

    def on_trial(event):
        outcomes.append(event.data)
        messages.append(TrialCompletedMessage.model_validate(event_to_wire(event)))
        received.set()

    bus.register_event_listener(EventType.CommandResult, on_command)
    bus.register_event_listener(EventType.TrialCompleted, results.on_trial_completed)
    bus.register_event_listener(EventType.TrialCompleted, on_trial)
    processor = SignalProcessor(
        None, None, Queue(), data, bus, generation=generation,
        config_revision=revision, settings_snapshot=snapshot,
    )
    await bus.start()
    try:
        processor._execute((SignalProcessorCommand.prepare_signal, CommandRequest("prepare", {
            "plan": plan.to_dict(), "receipt": receipt,
        })))
        confirmation = await asyncio.wait_for(prepared, timeout=2)
        assert confirmation.ok, confirmation.message
        assert confirmation.data == receipt
        assert processor.detector.plugin_type == "DEFAULT_EPOCH_DETECTOR"
        assert processor._decoder.plugin_type == "THUBCI_SSVEP_classic40CCA"

        times = np.arange(700) / sample_rate
        # Positive ADC counts avoid conflating this lifecycle regression with
        # the driver's separate signed-amplitude interpretation.
        eeg = np.asarray([
            np.rint(10_000 + 1_000 * np.sin(2 * np.pi * 8 * times + channel * 0.1))
            for channel in range(9)
        ], dtype="<u4")
        event_words = np.full(times.size, 0xABCD0000, dtype="<u4")
        event_words[25:30] |= 1
        raw = np.vstack((eeg, event_words))
        expected_events = np.zeros(times.size, dtype=int)
        expected_events[25:30] = 1

        # The protocol parser preserves the held pulse; the common acquisition
        # preprocessor emits its onset once even when the pulse spans packets.
        bounds = ((0, 27), (27, 300), (300, 700))
        parsed_chunks = []
        for start, end in bounds:
            body = raw[:, start:end].T.astype("<u4").tobytes()
            parsed = reader._parse_body_data(Scan45Message.Header("DATA", 2, 2, len(body)), body)
            np.testing.assert_array_equal(parsed[:-1], eeg[:, start:end])
            np.testing.assert_array_equal(parsed[-1], expected_events[start:end])
            parsed_chunks.append(parsed)

        monkeypatch.setattr(reader, "read_chunk", AsyncMock(side_effect=[*parsed_chunks, None]))
        monkeypatch.setattr(reader, "stop_stream", AsyncMock())
        acquisition = AcquisitionRuntime(
            reader, Queue(), data, bus, source_id=reader.plugin_type,
            preparation_required=False, generation=generation, config_revision=revision,
        )
        await acquisition.fetch()
        assert acquisition._fetch_failure is None
        assert [chunk.sequence_id for chunk in acquired] == [0, 1, 2]
        normalized_events = np.zeros(times.size, dtype=int)
        normalized_events[25] = 1
        for chunk, parsed, (start, end) in zip(acquired, parsed_chunks, bounds, strict=True):
            np.testing.assert_array_equal(parsed[-1], expected_events[start:end])
            np.testing.assert_array_equal(chunk.data, eeg[:, start:end])
            np.testing.assert_array_equal(chunk.triggers, normalized_events[start:end])
            mapped = plan.apply(chunk)
            np.testing.assert_array_equal(mapped.data, eeg[:, start:end])
            np.testing.assert_array_equal(mapped.triggers, normalized_events[start:end])
            await processor._current_state.run()

        await asyncio.wait_for(received.wait(), timeout=2)
        assert len(outcomes) == len(messages) == processor.completed_trials == 1
        outcome = outcomes[0]
        assert isinstance(outcome, TrialOutcome)
        assert outcome.generation == generation and outcome.trial_number == 1
        assert outcome.config_revision == revision
        assert outcome.classification.event_id == outcome.classification.target_id == 1
        assert outcome.classification.forced is False
        assert processor.current_epoch is None
        assert processor._epoch_number == 1
        assert data.empty()

        visible = results.snapshot()
        assert visible["generation"] == generation
        assert len(visible["trials"]) == 1
        assert visible["trials"][0]["event_id"] == visible["trials"][0]["target_id"] == 1
        message = messages[0]
        assert message.generation == generation and message.trial_number == 1
        assert message.event_id == message.target_id == 1
        assert message.config_revision == revision
    finally:
        await bus.stop()
