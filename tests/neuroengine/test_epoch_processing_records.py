"""Every terminal epoch can be inspected without changing decoded trial outputs."""

import asyncio
from dataclasses import asdict
import multiprocessing as mp
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from omnibci.application.events import EpochProcessed, StreamBarrier
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.signal_processing.signal_processor import (
    SignalProcessor, SignalProcessorCommand,
)
from omnibci.shared.events import EventType


class Markers(EpochDetector):
    def detect_start(self, chunk):
        indices = np.flatnonzero((chunk.triggers > 0) & (chunk.triggers < 255))
        return (int(indices[0]), int(chunk.triggers[indices[0]])) if indices.size else (-1, 0)

    def detect_end(self, chunk):
        indices = np.flatnonzero(chunk.triggers == 255)
        return (int(indices[0]), 255) if indices.size else (-1, 0)


class OptionalResult(Decoder):
    def __init__(self, *, ready=False, result=None, fail=False):
        self.ready = ready
        self.result = result
        self.fail = fail

    def should_decode(self, window):
        return self.ready

    def decode(self, window, latest_samples):
        if self.fail:
            raise RuntimeError("decoder failed")
        return self.result


def processor(decoder=None, *, event_bus=None):
    events = []
    value = SignalProcessor(
        Markers(), decoder or OptionalResult(), Queue(), DataManager(Queue()),
        event_bus or SimpleNamespace(put_event=events.append),
        sample_rate=10, baseline_duration=0.2,
        generation="generation", config_revision="revision",
    )
    return value, events


async def feed(value, values, markers, sequence=0):
    raw = np.vstack((values, markers))
    value.data_manager.put_data(SignalChunk(0, sequence, raw))
    await value._current_state.run()
    return raw


def records(events):
    return [event.data for event in events if event.type is EventType.EpochProcessed]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 100])
async def test_no_result_epochs_capture_every_boundary_and_pretrigger(chunk_size):
    value, events = processor()
    values = np.arange(1, 10)
    markers = [0, 0, 7, 0, 9, 0, 255, 10, 255]
    for offset in range(0, len(markers), chunk_size):
        await feed(value, values[offset:offset + chunk_size],
                   markers[offset:offset + chunk_size], sequence=offset)

    captured = records(events)
    assert [item.epoch_number for item in captured] == [1, 2, 3]
    assert [item.event_id for item in captured] == [7, 9, 10]
    assert [item.reason for item in captured] == ["next_start", "end_marker", "end_marker"]
    assert all(item.status == "no_result" for item in captured)
    assert all(item.classification is None and item.trial_number is None for item in captured)
    for item, samples in zip(captured, [2, 2, 1], strict=True):
        assert item.pre_trigger_samples == 2
        assert item.sample_count == samples
        assert item.channel_count == 1
        assert item.sample_rate == 10
        assert item.started_at <= item.completed_at
    assert value.completed_trials == 0
    assert value.current_epoch is None


@pytest.mark.asyncio
async def test_result_follows_no_result_with_separate_trial_and_epoch_numbers():
    decoder = OptionalResult()
    value, events = processor(decoder)
    await feed(value, [1, 2], [7, 255])
    decoder.result = Prediction(3, confidence=0.8)
    decoder.ready = True
    await feed(value, [3, 4], [9, 0], sequence=1)
    await feed(value, [5, 6], [0, 0], sequence=2)

    captured = records(events)
    assert [item.epoch_number for item in captured] == [1, 2]
    assert [item.trial_number for item in captured] == [None, 1]
    assert captured[1].status == "decoded"
    assert captured[1].reason == "prediction"
    assert [event.type for event in events] == [
        EventType.EpochProcessed, EventType.TrialCompleted, EventType.EpochProcessed,
    ]
    assert captured[1].classification is events[1].data.classification
    assert value.completed_trials == 1


@pytest.mark.asyncio
async def test_boundary_result_keeps_forced_classification_and_signal_counts():
    value, events = processor(OptionalResult(result=Prediction(2)))
    raw = await feed(value, [1, 2, 3], [7, 0, 255])
    captured = records(events)[0]
    raw[:] = -1

    assert captured.status == "decoded"
    assert captured.reason == "end_marker"
    assert captured.classification.forced is True
    assert captured.sample_count == 2
    assert captured.channel_count == 1
    assert captured.pre_trigger_samples == 0
    assert [event.type for event in events] == [EventType.TrialCompleted, EventType.EpochProcessed]


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,result,status,trial_number", [
    (False, None, "interrupted", None),
    (True, None, "interrupted", None),
    (True, Prediction(2), "decoded", 1),
])
async def test_drain_records_epoch_before_receipt_and_does_not_repeat_on_stop(
    ready, result, status, trial_number,
):
    value, events = processor(OptionalResult(ready=ready, result=result))
    await feed(value, [1, 2], [7, 0])
    value.data_manager.put_data(StreamBarrier("drain", "generation", "revision", 0))
    await value._current_state.run()
    value.command_queue.put(SignalProcessorCommand.should_stop)
    await value.serve()

    captured = records(events)
    assert len(captured) == 1
    assert captured[0].status == status
    assert captured[0].reason == "stream_stop"
    assert captured[0].trial_number == trial_number
    assert events[-1].type is EventType.CommandResult
    assert events[-1].data.ok


@pytest.mark.asyncio
async def test_direct_state_error_records_failure_and_serve_exit_does_not_duplicate():
    value, events = processor(OptionalResult(fail=True))
    with pytest.raises(RuntimeError, match="decoder failed"):
        await feed(value, [1, 2], [7, 255])
    value.command_queue.put(SignalProcessorCommand.should_stop)
    await value.serve()

    captured = records(events)
    assert len(captured) == 1
    assert captured[0].status == "error"
    assert captured[0].error == "decoder failed"
    assert captured[0].sample_count == 1
    assert value.completed_trials == 0


@pytest.mark.asyncio
async def test_per_epoch_decoder_creation_failure_is_recorded_without_signal_data():
    class CreationFails(OptionalResult):
        def for_new_epoch(self):
            raise RuntimeError("new epoch decoder failed")

    value, events = processor(CreationFails())
    with pytest.raises(RuntimeError, match="new epoch decoder failed"):
        await feed(value, [1, 2], [7, 0])

    captured = records(events)
    assert len(captured) == 1
    assert captured[0].event_id == 7
    assert captured[0].status == "error"
    assert captured[0].error == "new epoch decoder failed"
    assert captured[0].sample_count == 0
    assert captured[0].channel_count == 1
    assert captured[0].pre_trigger_samples == 0
    assert value.current_epoch is None


@pytest.mark.asyncio
async def test_drain_error_is_recorded_once_even_if_drain_is_retried():
    value, events = processor(OptionalResult(ready=True, fail=True))
    await feed(value, [1, 2], [7, 0])
    for command in ("first", "retry"):
        value.data_manager.put_data(StreamBarrier(command, "generation", "revision", 0))
        await value._current_state.run()

    captured = records(events)
    assert len(captured) == 1
    assert captured[0].status == "error"
    assert captured[0].reason == "error"
    assert captured[0].error == "decoder failed"
    receipts = [event.data for event in events if event.type is EventType.CommandResult]
    assert len(receipts) == 2
    assert all(not receipt.ok for receipt in receipts)


@pytest.mark.asyncio
async def test_direct_processor_stop_records_interrupted_active_epoch():
    value, events = processor()
    await feed(value, [1, 2], [7, 0])
    value.command_queue.put(SignalProcessorCommand.should_stop)
    await value.serve()

    assert len(records(events)) == 1
    assert records(events)[0].status == "interrupted"
    assert records(events)[0].reason == "processor_stop"


@pytest.mark.asyncio
async def test_first_success_remains_when_second_epoch_in_same_chunk_fails():
    class SecondFails(OptionalResult):
        def decode(self, window, latest_samples):
            if window[0, 0] == 3:
                raise RuntimeError("second epoch failed")
            return Prediction(2)

    value, events = processor(SecondFails())
    with pytest.raises(RuntimeError, match="second epoch failed"):
        await feed(value, [1, 2, 3, 4], [7, 255, 9, 255])

    captured = records(events)
    assert [item.status for item in captured] == ["decoded", "error"]
    assert [item.epoch_number for item in captured] == [1, 2]
    assert [item.trial_number for item in captured] == [1, None]
    assert [item.sample_count for item in captured] == [1, 1]
    assert [item.pre_trigger_samples for item in captured] == [0, 2]


@pytest.mark.asyncio
async def test_metadata_stays_bound_to_open_epoch():
    value, events = processor()
    await feed(value, [1, 2], [7, 0])
    value.config_revision = "later-revision"
    value._sample_rate = 100
    await feed(value, [3], [255], sequence=1)

    captured = records(events)[0]
    assert captured.config_revision == "revision"
    assert captured.sample_rate == 10
    assert captured.channel_count == 1


@pytest.mark.asyncio
async def test_terminal_record_never_builds_or_retains_signal_snapshot(monkeypatch):
    value, events = processor()
    await feed(value, [1, 2], [7, 0])

    build_epoch = Mock(side_effect=AssertionError("Signal snapshot must not be constructed"))
    monkeypatch.setattr(value.current_epoch, "build_epoch", build_epoch)
    await feed(value, [3], [255], sequence=1)

    captured = records(events)[0]
    assert captured.status == "no_result"
    assert captured.sample_count == 2
    assert captured.error is None
    build_epoch.assert_not_called()
    assert set(asdict(captured)) == {
        "generation", "epoch_number", "event_id", "config_revision", "sample_rate",
        "channel_count", "sample_count", "pre_trigger_samples", "started_at", "completed_at",
        "status", "reason", "classification", "trial_number", "error",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm_fails", [False, True])
async def test_diagnostic_publication_failure_does_not_change_processing_outcome(algorithm_fails):
    events = []

    def publish(event):
        if event.type is EventType.EpochProcessed:
            raise OSError("inspection transport failed")
        events.append(event)

    value, _ = processor(
        OptionalResult(result=Prediction(2), fail=algorithm_fails),
        event_bus=SimpleNamespace(put_event=publish),
    )
    if algorithm_fails:
        with pytest.raises(RuntimeError, match="decoder failed"):
            await feed(value, [1, 2], [7, 255])
        assert events == []
    else:
        await feed(value, [1, 2], [7, 255])
        assert value.completed_trials == 1
        assert value.current_epoch is None
        assert [event.type for event in events] == [EventType.TrialCompleted]


def publish_in_spawn(event_queue):
    value, _ = processor(event_bus=EventBus(event_queue))
    asyncio.run(feed(value, [1, 2, 3], [7, 0, 255]))


def test_epoch_record_crosses_real_spawn_queue():
    context = mp.get_context("spawn")
    event_queue = context.Queue()
    worker = context.Process(target=publish_in_spawn, args=(event_queue,))
    worker.start()
    try:
        event = event_queue.get(timeout=10)
        worker.join(timeout=10)
        assert worker.exitcode == 0
        assert event.type is EventType.EpochProcessed
        assert isinstance(event.data, EpochProcessed)
        assert event.data.generation == "generation"
        assert event.data.sample_count == 2
        assert event.data.channel_count == 1
        assert event.data.pre_trigger_samples == 0
        assert not hasattr(event.data, "epoch")
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
        event_queue.close()
        event_queue.join_thread()
