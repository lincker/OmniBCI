"""Epoch boundaries depend on marker order, not acquisition chunk sizes."""

from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from omnibci.application.events import CommandRequest
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.neuroengine.signal_processing import configuration_transactions as transactions_module
from omnibci.infrastructure.neuroengine.signal_processing.pipeline_configuration import ConfiguredPipeline
from omnibci.infrastructure.neuroengine.signal_processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.shared.events import EventType


class Markers(EpochDetector):
    def detect_start(self, chunk):
        positions = np.flatnonzero((chunk.triggers > 0) & (chunk.triggers < 255))
        return (int(positions[0]), int(chunk.triggers[positions[0]])) if positions.size else (-1, 0)

    def detect_end(self, chunk):
        positions = np.flatnonzero(chunk.triggers == 255)
        return (int(positions[0]), 255) if positions.size else (-1, 0)


class RecordingDecoder(Decoder):
    def __init__(self, instances):
        self.instances = instances
        self.windows = []
        self.latest_samples = []

    def for_new_epoch(self):
        decoder = RecordingDecoder(self.instances)
        self.instances.append(decoder)
        return decoder

    def should_decode(self, window):
        return False

    def decode(self, window, latest_samples):
        self.windows.append(window.copy())
        self.latest_samples.append(latest_samples.copy())
        return Prediction(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 100])
@pytest.mark.parametrize("markers,expected", [
    ([7, 0, 255, 0], [(7, [1, 2])]),
    ([7, 0, 9, 0, 255], [(7, [1, 2]), (9, [3, 4])]),
    ([7, 255, 9, 255], [(7, [1]), (9, [3])]),
    ([255, 0, 7, 0, 255], [(7, [3, 4])]),
    ([7, 0, 255, 9, 0, 255, 0], [(7, [1, 2]), (9, [4, 5])]),
])
async def test_all_markers_form_the_same_epochs_for_any_chunk_size(markers, expected, chunk_size):
    instances = []
    processor = SignalProcessor(
        detector=Markers(), decoder=RecordingDecoder(instances), command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()), event_bus=SimpleNamespace(put_event=lambda event: None),
        sample_rate=1000,
    )
    raw = np.vstack((np.arange(1, len(markers) + 1), markers))
    original = raw.copy()
    for offset in range(0, len(markers), chunk_size):
        processor.data_manager.put_data(SignalChunk(1.0, offset, raw[:, offset:offset + chunk_size]))
        await processor._current_state.run()

    assert [trial.classification.event_id for trial in processor.session.trials] == [item[0] for item in expected]
    assert len(instances) == len(expected)
    for decoder, (_, samples) in zip(instances, expected, strict=True):
        assert len(decoder.windows) == 1
        np.testing.assert_array_equal(decoder.windows[0], [samples])
    assert processor.current_epoch is None
    assert processor._current_state is processor._awaiting_state
    np.testing.assert_array_equal(raw, original)


class CaptureBaseline(Filter):
    def __init__(self):
        self.baselines = []

    def apply(self, data, context):
        self.baselines.append(context.baseline.copy())
        return data


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 100])
@pytest.mark.parametrize("markers,expected", [
    ([0, 0, 7, 0, 255, 0, 9, 255], [[1, 2], [4, 5, 6]]),
    ([0, 0, 7, 0, 9, 255], [[1, 2], [2, 3, 4]]),
])
async def test_reopened_epoch_baselines_are_independent_of_chunk_size(markers, expected, chunk_size):
    baseline = CaptureBaseline()
    processor = SignalProcessor(
        detector=Markers(), decoder=RecordingDecoder([]), command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()), event_bus=SimpleNamespace(put_event=lambda event: None),
        sample_rate=1000, baseline_duration=0.003, filters=[baseline],
    )
    raw = np.vstack((np.arange(1, len(markers) + 1), markers))
    for offset in range(0, len(markers), chunk_size):
        processor.data_manager.put_data(SignalChunk(1.0, offset, raw[:, offset:offset + chunk_size]))
        await processor._current_state.run()

    assert len(baseline.baselines) == len(expected)
    for captured, samples in zip(baseline.baselines, expected, strict=True):
        np.testing.assert_array_equal(captured, [samples])


@pytest.mark.asyncio
async def test_staged_configuration_applies_between_multiple_epochs_in_one_chunk(monkeypatch):
    events, original_instances, replacement_instances = [], [], []
    processor = SignalProcessor(
        detector=Markers(), decoder=RecordingDecoder(original_instances), command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()), event_bus=SimpleNamespace(put_event=events.append),
        sample_rate=1000, generation="run", config_revision="old", settings_snapshot={"CONFIG.revision": "old"},
    )
    processor.data_manager.put_data(SignalChunk(1.0, 0, np.array([[1, 2], [7, 0]])))
    await processor._current_state.run()
    candidate = ConfiguredPipeline(RecordingDecoder(replacement_instances), (), {"CONFIG.revision": "new"})
    build = MagicMock(return_value=candidate)
    monkeypatch.setattr(transactions_module, "build_pipeline_update", build)
    processor._execute((SignalProcessorCommand.stage_configuration, CommandRequest("update", {
        "settings_snapshot": candidate.settings_snapshot, "expected_revision": "old", "generation": "run",
    })))
    processor.data_manager.put_data(SignalChunk(2.0, 1, np.array([[3, 4, 5, 6, 7], [255, 9, 255, 10, 255]])))
    await processor._current_state.run()

    assert [trial.classification.event_id for trial in processor.session.trials] == [7, 9, 10]
    assert [trial.config_revision for trial in processor.session.trials] == ["old", "new", "new"]
    ordered = [event for event in events if event.type in (EventType.TrialCompleted, EventType.CommandResult)]
    assert [event.type for event in ordered] == [EventType.TrialCompleted, EventType.CommandResult,
                                                EventType.TrialCompleted, EventType.TrialCompleted]
    assert ordered[1].data.ok
    assert len(original_instances) == 1
    assert len(replacement_instances) == 2
    # The old epoch closes at chunk index 0, so its latest segment stays empty.
    np.testing.assert_array_equal(original_instances[0].windows[0], [[1, 2]])
    assert original_instances[0].latest_samples[0].shape == (1, 0)
    build.assert_called_once()


@pytest.mark.asyncio
async def test_simultaneous_end_and_start_closes_previous_epoch_before_reopening():
    class SimultaneousMarkers(Markers):
        def detect_end(self, chunk):
            positions = np.flatnonzero((chunk.triggers == 9) | (chunk.triggers == 255))
            return (int(positions[0]), 255) if positions.size else (-1, 0)

    instances = []
    processor = SignalProcessor(
        detector=SimultaneousMarkers(), decoder=RecordingDecoder(instances), command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()), event_bus=SimpleNamespace(put_event=lambda event: None),
        sample_rate=1000,
    )
    processor.data_manager.put_data(SignalChunk(1.0, 0, np.array([[1, 2, 3, 4, 5], [7, 0, 9, 0, 255]])))
    await processor._current_state.run()

    assert [trial.classification.event_id for trial in processor.session.trials] == [7, 9]
    np.testing.assert_array_equal(instances[0].windows[0], [[1, 2]])
    np.testing.assert_array_equal(instances[1].windows[0], [[3, 4]])
    assert processor.current_epoch is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["detect_start", "detect_end"])
async def test_detector_boundary_outside_chunk_is_rejected(method):
    detector = MagicMock(spec=EpochDetector)
    detector.detect_start.return_value = (-1, 0)
    detector.detect_end.return_value = (-1, 0)
    getattr(detector, method).return_value = (3, 7)
    processor = SignalProcessor(
        detector=detector, decoder=RecordingDecoder([]), command_queue=Queue(),
        data_manager=DataManager(buffer=Queue()), event_bus=SimpleNamespace(put_event=lambda event: None),
    )
    processor.data_manager.put_data(SignalChunk(1.0, 0, np.array([[1, 2, 3], [0, 0, 0]])))

    with pytest.raises(ValueError, match="boundary outside the signal chunk"):
        await processor._current_state.run()

    assert processor.current_epoch is None
    assert processor.session.trial_count == 0
