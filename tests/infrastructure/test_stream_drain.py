"""Ordered acquisition/processing draining and recording projection evidence."""

from __future__ import annotations

import asyncio
import pickle
import queue
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from omnibci.application.events import (
    CommandFailureKind, CommandRequest, CommandResult, StreamBarrier, StreamDrainReceipt,
)
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import CommandResultBroker, CommandTimeoutError
from omnibci.application.services.recording_service import RecordingDrainError, RecordingService
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Prediction
from omnibci.domain.repositories import SessionRecord, SessionRecordState
from omnibci.domain.session import BCISession
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime, AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.signal_processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.infrastructure.repositories.session_repository import InMemorySessionRepository
from omnibci.shared.events import Event, EventType


class _Bus:
    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


class _Source:
    is_connected = True
    is_streaming = True

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False


class _Detector(EpochDetector):
    def detect_start(self, chunk):
        starts = np.flatnonzero(chunk.triggers == 1)
        return (int(starts[0]), 1) if len(starts) else (-1, 0)

    def detect_end(self, chunk):
        return -1, 0


class _Decoder(Decoder):
    def __init__(self, samples=4, delay=0, decline=False, fail=False):
        self.samples = samples
        self.delay = delay
        self.decline = decline
        self.fail = fail
        self.calls = 0

    def should_decode(self, window):
        return window.shape[1] >= self.samples

    def decode(self, window, latest_samples):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("decoder failed")
        return None if self.decline else Prediction(1)


def _chunk(sequence, *, start=False, samples=2):
    raw = np.ones((2, samples))
    raw[-1] = 0
    if start:
        raw[-1, 0] = 1
    return SignalChunk(0, sequence, raw)


def _processor(*, decoder=None, bus=None, data=None, session=None):
    if session is None:
        session = BCISession(session_id="session-1")
        session.start()
        session.pop_events()
    return SignalProcessor(
        _Detector(), decoder or _Decoder(), queue.Queue(), data or DataManager(queue.Queue()),
        bus or _Bus(), session=session, sample_rate=10, baseline_duration=0.2,
        generation="generation-1", config_revision="revision-1",
    )


def _barrier(sequence=-1, **overrides):
    return replace(StreamBarrier("drain-1", "generation-1", "revision-1", sequence, "session-1"), **overrides)


@pytest.mark.asyncio
async def test_acquisition_joins_producer_before_in_band_marker_and_defers_success():
    data = DataManager(queue.Queue())
    bus = _Bus()
    acquisition = AcquisitionRuntime(
        _Source(), queue.Queue(), data, bus,
        source_id="test-source", preparation_required=False,
        generation="generation-1", config_revision="revision-1",
    )
    started = asyncio.Event()

    async def producer():
        data.put_data(_chunk(0))
        acquisition._last_sequence = 0
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Even a producer's final cleanup write must precede the barrier.
            data.put_data(_chunk(1))
            acquisition._last_sequence = 1

    acquisition.fetch_task = asyncio.create_task(producer())
    await started.wait()
    producer_task = acquisition.fetch_task

    await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain-1", {"session_id": "session-1"})))

    assert producer_task.done()
    assert acquisition.fetch_task is None
    assert data.get_data(0).sequence_id == 0
    assert data.get_data(0).sequence_id == 1
    assert data.get_data(0) == _barrier(1)
    assert not [event for event in bus.events if event.type == EventType.CommandResult]


@pytest.mark.asyncio
async def test_stop_failure_joins_fetch_and_emits_failure_without_barrier():
    class Source(_Source):
        async def stop_stream(self):
            raise OSError("device stop failed")

    data = DataManager(queue.Queue())
    bus = _Bus()
    acquisition = AcquisitionRuntime(
        Source(), queue.Queue(), data, bus,
        source_id="test-source", preparation_required=False,
    )
    acquisition.fetch_task = asyncio.create_task(asyncio.Event().wait())
    task = acquisition.fetch_task

    await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain-error")))

    assert task.done()
    assert data.get_data(0) is None
    result = bus.events[-1].data
    assert result.command == "StopAndDrain"
    assert not result.ok
    assert result.failure_kind is CommandFailureKind.EXECUTION_FAILED


@pytest.mark.asyncio
async def test_acquisition_shutdown_cancellation_emits_failure_not_success():
    entered = asyncio.Event()

    class Source(_Source):
        async def stop_stream(self):
            entered.set()
            await asyncio.Event().wait()

    data = DataManager(queue.Queue())
    bus = _Bus()
    acquisition = AcquisitionRuntime(
        Source(), queue.Queue(), data, bus,
        source_id="test-source", preparation_required=False,
    )
    task = asyncio.create_task(acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("cancelled"))))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert data.get_data(0) is None
    assert bus.events[-1].data.error_code == "ACQUISITION_CANCELLED"
    assert not bus.events[-1].data.ok


@pytest.mark.asyncio
async def test_barrier_finishes_first_chunk_when_ready_without_forcing_partial_data():
    decoder = _Decoder(samples=2)
    processor = _processor(decoder=decoder)
    processor.data_manager.put_data(_chunk(0, start=True))
    processor.data_manager.put_data(_barrier(0))

    await processor._current_state.run()
    assert decoder.calls == 0
    await processor._current_state.run()

    assert decoder.calls == 1
    assert processor.session.trial_count == 1
    events = processor.event_bus.events
    assert [event.type for event in events] == [EventType.TrialCompleted, EventType.CommandResult]
    assert events[0].data.classification.forced is False
    receipt = StreamDrainReceipt.from_value(events[-1].data.data)
    assert receipt.completed_trials == 1
    assert receipt.interrupted_epoch is False
    assert receipt.outputs_settled is False
    assert processor.current_epoch is None


@pytest.mark.asyncio
async def test_standby_barrier_discards_decoded_epoch_without_recording_a_trial():
    processor = _processor(decoder=_Decoder(samples=2), session=BCISession())
    processor.data_manager.put_data(_chunk(0, start=True))
    processor.data_manager.put_data(_barrier(0, session_id=None))

    await processor._current_state.run()
    await processor._current_state.run()

    assert processor.session.trial_count == 0
    assert [event.type for event in processor.event_bus.events] == [EventType.CommandResult]
    result = processor.event_bus.events[-1].data
    assert result.ok
    receipt = StreamDrainReceipt.from_value(result.data)
    assert receipt.session_id is None
    assert receipt.completed_trials == 0
    assert receipt.interrupted_epoch is False
    assert processor.current_epoch is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["record", "publish"])
async def test_trial_completion_failure_at_barrier_cannot_be_retried_as_success(failure, monkeypatch):
    decoder = _Decoder(samples=2)
    processor = _processor(decoder=decoder)
    processor.data_manager.put_data(_chunk(0, start=True))
    await processor._current_state.run()
    current_epoch = processor.current_epoch
    record_trial = MagicMock(wraps=processor.session.record_trial)
    publish_all = MagicMock(wraps=processor.event_publisher.publish_all)
    monkeypatch.setattr(processor.session, "record_trial", record_trial)
    monkeypatch.setattr(processor.event_publisher, "publish_all", publish_all)
    error = RuntimeError("record failed") if failure == "record" else OSError("publish failed")
    failed_operation = record_trial if failure == "record" else publish_all
    failed_operation.side_effect = error

    for command_id in ("initial", "retry"):
        processor.data_manager.put_data(_barrier(0, command_id=command_id))
        await processor._current_state.run()

        result = processor.event_bus.events[-1].data
        assert not result.ok
        assert result.command_id == command_id
        assert result.failure_kind is CommandFailureKind.EXECUTION_FAILED
        assert result.error_code == "STREAM_DRAIN_FAILED"
        assert result.message == str(error)
        assert result.data is None
        assert processor.current_epoch is current_epoch
        assert processor._current_state is processor._assembling_state
        assert decoder.calls == 1
        assert record_trial.call_count == 1
        assert publish_all.call_count == (1 if failure == "publish" else 0)
        assert processor.session.trial_count == (1 if failure == "publish" else 0)
        # Even recovery of the failing dependency cannot undo a partial attempt.
        failed_operation.side_effect = None


@pytest.mark.asyncio
async def test_incomplete_epoch_and_pretrigger_are_discarded_with_diagnostic_receipt():
    decoder = _Decoder(samples=100)
    processor = _processor(decoder=decoder)
    processor.data_manager.put_data(_chunk(0, start=True))
    await processor._current_state.run()
    processor.pretrigger.push(np.ones((1, 2)))
    processor.data_manager.put_data(_barrier(0))

    await processor._current_state.run()

    assert decoder.calls == 0
    assert processor.session.trial_count == 0
    assert processor.current_epoch is None
    assert processor._current_state is processor._awaiting_state
    assert processor.pretrigger.take_pre_trigger_data(np.empty((1, 0))).shape[1] == 0
    receipt = StreamDrainReceipt.from_value(processor.event_bus.events[-1].data.data)
    assert receipt.interrupted_epoch is True


@pytest.mark.asyncio
async def test_barrier_does_not_feed_an_incremental_decoder_the_same_latest_samples_twice():
    decoder = _Decoder(samples=4, decline=True)
    processor = _processor(decoder=decoder)
    for item in (_chunk(0, start=True), _chunk(1), _barrier(1)):
        processor.data_manager.put_data(item)
        await processor._current_state.run()

    assert decoder.calls == 1
    assert StreamDrainReceipt.from_value(processor.event_bus.events[-1].data.data).interrupted_epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"generation": "stale"}, {"config_revision": "stale"},
    {"last_sequence": 9}, {"session_id": "stale"},
])
async def test_stale_or_inconsistent_barriers_fail_without_claiming_drained(overrides):
    processor = _processor()
    processor.data_manager.put_data(_barrier(**overrides))

    assert await processor.next_chunk() is None

    result = processor.event_bus.events[-1].data
    assert not result.ok
    assert result.error_code == "STREAM_DRAIN_FAILED"
    assert result.data is None


@pytest.mark.asyncio
async def test_decoder_error_at_barrier_returns_execution_failure():
    processor = _processor(decoder=_Decoder(samples=2, fail=True))
    processor.data_manager.put_data(_chunk(0, start=True))
    await processor._current_state.run()
    processor.data_manager.put_data(_barrier(0))

    await processor._current_state.run()

    result = processor.event_bus.events[-1].data
    assert result.failure_kind is CommandFailureKind.EXECUTION_FAILED
    assert not result.ok
    assert processor.session.trial_count == 0
    processor.data_manager.put_data(_barrier(0, command_id="retry"))
    await processor._current_state.run()
    assert not processor.event_bus.events[-1].data.ok
    assert processor.event_bus.events[-1].data.command_id == "retry"


@pytest.mark.asyncio
async def test_acquisition_sequence_remains_monotonic_across_stream_restarts():
    class Source(_Source):
        pending = None

        async def read_chunk(self):
            result, self.pending = self.pending, None
            return result

    source = Source()
    data = DataManager(queue.Queue())
    acquisition = AcquisitionRuntime(
        source, queue.Queue(), data,
        source_id="test-source", preparation_required=False,
    )
    for _ in range(2):
        source.pending = np.zeros((2, 2))
        await acquisition.fetch()

    assert data.get_data(0).sequence_id == 0
    assert data.get_data(0).sequence_id == 1
    assert acquisition._last_sequence == 1


@pytest.mark.asyncio
async def test_shutdown_joins_pending_queue_read_and_rejects_consumed_barrier():
    entered = threading.Event()
    release = threading.Event()

    class PendingQueue:
        def get_data(self, timeout):
            entered.set()
            release.wait(2)
            return _barrier()

    processor = _processor(data=PendingQueue())
    task = asyncio.create_task(processor.next_chunk())
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    result = processor.event_bus.events[-1].data
    assert result.error_code == "PROCESSOR_CANCELLED"
    assert not result.ok


@pytest.mark.asyncio
@pytest.mark.parametrize("projection_fails", [False, True])
async def test_recording_waits_for_slow_backlog_and_checks_actual_projection(projection_fails):
    event_bus = EventBus(queue.Queue())
    data = DataManager(queue.Queue())
    processor = _processor(decoder=_Decoder(delay=0.4), bus=event_bus, data=data)
    acquisition = AcquisitionRuntime(
        _Source(), queue.Queue(), data, event_bus,
        source_id="test-source", preparation_required=False,
        generation="generation-1", config_revision="revision-1",
    )
    acquisition._last_sequence = 1
    data.put_data(_chunk(0, start=True))
    data.put_data(_chunk(1))
    repository = InMemorySessionRepository()
    repository.save(SessionRecord("session-1", None, SessionRecordState.RUNNING, datetime.now(UTC)))
    sessions = SessionAppService(repository)
    broker = CommandResultBroker(SimpleNamespace(dispatch=event_bus.put_event, is_engine_ready=True), timeout_s=3)
    recording = RecordingService(sessions, AcquisitionAppService(broker))
    event_bus.register_event_listener(EventType.CommandResult, broker.on_result)

    def project(event):
        if projection_fails:
            raise OSError("controlled projection write failure")
        sessions.on_trial_completed(event)

    async def stop(event):
        await acquisition.execute((AcquisitionCommand.stop_and_drain, event.data))

    event_bus.register_event_listener(EventType.TrialCompleted, project)
    event_bus.register_event_listener(EventType.StopAndDrain, stop)
    await event_bus.start()
    # Queue the request before allowing processing to consume the backlog.
    request = asyncio.create_task(asyncio.to_thread(recording.stop))
    await asyncio.sleep(0.02)
    started = time.monotonic()
    processing_task = asyncio.create_task(processor.serve())
    try:
        if projection_fails:
            with pytest.raises(RecordingDrainError, match="projection is incomplete"):
                await request
            assert sessions.active_session_id == "session-1"
            assert repository.find_by_id("session-1").state is SessionRecordState.RUNNING
        else:
            assert await request == "session-1"
            record = repository.find_by_id("session-1")
            assert record.state is SessionRecordState.ENDED
            assert record.trial_count == 1
        assert time.monotonic() - started >= 0.4
        assert processor.session.trial_count == 1
    finally:
        processor._should_stop = True
        await processing_task
        await event_bus.stop()


def test_recording_drain_timeout_keeps_session_open():
    sessions = MagicMock(active_session_id="session-1")
    acquisition = MagicMock()
    acquisition.handle_stop_and_drain.side_effect = CommandTimeoutError("id", "StopAndDrain", 0.1)

    with pytest.raises(CommandTimeoutError):
        RecordingService(sessions, acquisition).stop()

    sessions.handle_end_session.assert_not_called()


def test_duplicate_projection_cannot_hide_a_missing_trial_with_same_total_count():
    sessions = MagicMock(active_session_id="session-1")
    sessions.handle_get_trials.return_value = [SimpleNamespace(trial_number=1), SimpleNamespace(trial_number=1)]
    acquisition = MagicMock()
    acquisition.handle_stop_and_drain.return_value = StreamDrainReceipt("g", "r", 3, "session-1", 2, False)

    with pytest.raises(RecordingDrainError):
        RecordingService(sessions, acquisition).stop()

    sessions.handle_end_session.assert_not_called()


def test_acquisition_service_requires_structured_evidence_and_preserves_deadline():
    receipt = StreamDrainReceipt("g", "r", 3, "session-1", 1, True)
    executor = MagicMock()
    executor.request.return_value = CommandResult("id", "StopAndDrain", True, data=receipt.to_dict())
    service = AcquisitionAppService(executor)

    assert service.handle_stop_and_drain(session_id="session-1", timeout_s=42) == receipt
    executor.request.assert_called_once_with(EventType.StopAndDrain, {"session_id": "session-1"}, timeout_s=42)
    executor.request.return_value = CommandResult("id", "StopAndDrain", True)
    with pytest.raises(RuntimeError, match="valid stream drain evidence"):
        service.handle_stop_and_drain()


def test_drain_payloads_are_pickle_safe_and_result_mapping_preserves_receipt():
    barrier = _barrier()
    receipt = StreamDrainReceipt("g", "r", 1, "session-1", 1, False)
    result = CommandResult("id", "StopAndDrain", True, data=receipt.to_dict())
    assert pickle.loads(pickle.dumps(barrier)) == barrier
    assert CommandResult.from_value(asdict(result)) == result
    assert CommandResult.from_value({**asdict(result), "data": []}) is None
    with pytest.raises(ValueError):
        StreamDrainReceipt.from_value({**receipt.to_dict(), "outputs_settled": True})


@pytest.mark.asyncio
async def test_shutdown_command_does_not_consume_data_after_session_end():
    processor = _processor()
    processor.command_queue.put(SignalProcessorCommand.should_stop)
    processor.data_manager.put_data(_chunk(0, start=True))

    await processor.serve()

    assert processor.data_manager.get_data(0).sequence_id == 0
    assert processor.current_epoch is None
