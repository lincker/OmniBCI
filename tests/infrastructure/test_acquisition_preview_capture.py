"""Acquisition previews never become a second signal consumer."""
import asyncio
import pickle
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from omnibci.application.acquisition_preview import (
    MAX_CACHE_BYTES, MAX_ROWS, MAX_SAMPLES, MAX_VALUES, PreviewSelection,
)
from omnibci.application.events import CommandRequest
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.acquisition.preview_capture import PreviewCapture
from omnibci.infrastructure.ipc.data_manager import DataManager


IDENTITY = {"generation": "run", "config_revision": "revision", "connection_id": "connection",
            "source_id": "source"}


def chunk(sequence=0, samples=5, dtype=np.int32):
    return SignalChunk(1234567890123456789, sequence, np.arange(3 * samples, dtype=dtype).reshape(3, samples))


def capture(*, queue=None, selection=None, clock=None):
    frames = Queue() if queue is None else queue
    producer = PreviewCapture(frames, **({"clock": clock} if clock else {}))
    producer.start("capture", selection or PreviewSelection(), IDENTITY)
    return producer, frames


def test_capture_stops_after_32_observed_chunks_even_if_queue_is_full():
    producer, frames = capture(queue=Queue(maxsize=1))
    for sequence in range(40):
        producer.offer(chunk(sequence), IDENTITY)
    status = producer.snapshot()
    assert status["status"] == "finished" and status["reason"] == "chunk_limit"
    assert (status["observed"], status["enqueued"], status["dropped"]) == (32, 1, 31)
    assert frames.qsize() == 1


def test_selection_is_owned_bounded_and_preserves_trigger_and_short_tail():
    producer, frames = capture(selection=PreviewSelection(rows=(1, 0), sample_start=3, sample_count=5))
    original = chunk()
    expected = original.raw_data[[1, 0, 2], 3:].copy()
    producer.offer(original, IDENTITY, descriptor={"source_id": "source"})
    original.raw_data[:] = -1
    frame = pickle.loads(frames.get_nowait())
    assert frame["row_indices"] == [1, 0, 2]
    assert frame["original_shape"] == [3, 5]
    assert frame["sample_start"] == 3 and frame["sample_count"] == 2
    assert frame["timestamp_ns"] == "1234567890123456789"
    assert frame["trigger_row_index"] == 2
    assert frame["descriptor"] == {"source_id": "source"}
    np.testing.assert_array_equal(frame["values"], expected)


def test_sample_start_beyond_a_short_chunk_has_an_explicit_empty_slice():
    producer, frames = capture(selection=PreviewSelection(sample_start=20))
    producer.offer(chunk(), IDENTITY)
    frame = pickle.loads(frames.get_nowait())
    assert frame["sample_start"] == 20 and frame["sample_count"] == 0
    assert frame["values"].shape == (2, 0)


def test_deadline_expires_without_observing_any_chunk_and_result_is_retained():
    now = [1.0]
    producer, _ = capture(clock=lambda: now[0])
    now[0] = 11.0
    producer.expire()
    assert producer.snapshot()["reason"] == "timeout"
    assert producer.snapshot()["observed"] == 0
    now[0] = 100.0
    assert producer.snapshot()["capture_id"] == "capture"


def test_a_second_start_is_rejected_and_stale_stop_cannot_end_current_capture():
    producer, _ = capture()
    with pytest.raises(ValueError, match="active"):
        producer.start("other", PreviewSelection(), IDENTITY)
    with pytest.raises(ValueError, match="capture"):
        producer.stop("other")
    assert producer.snapshot()["status"] == "capturing"
    producer.stop("capture")
    producer.stop("capture")
    assert producer.snapshot()["reason"] == "user_stop"
    producer.start("other", PreviewSelection(), IDENTITY)
    assert producer.snapshot()["observed"] == 0


@pytest.mark.parametrize("field", ["generation", "config_revision", "connection_id", "source_id"])
def test_frames_from_a_read_started_in_another_context_are_ignored(field):
    producer, frames = capture()
    producer.offer(chunk(), {**IDENTITY, field: "old"})
    assert frames.empty() and producer.snapshot()["observed"] == 0


@pytest.mark.parametrize("selection,dtype,reason", [
    (PreviewSelection(rows=(2,)), np.int32, "invalid_selection"),
    (PreviewSelection(), np.complex128, "unsupported_dtype"),
])
def test_invalid_rows_and_unsupported_dtype_only_finish_preview(selection, dtype, reason):
    producer, frames = capture(selection=selection)
    producer.offer(chunk(dtype=dtype), IDENTITY)
    assert producer.snapshot()["reason"] == reason
    assert frames.empty()


def test_oversized_encoded_frame_is_dropped_without_extending_window():
    producer, frames = capture(selection=PreviewSelection(chunk_count=1))
    producer.offer(chunk(), IDENTITY, descriptor={"description": "x" * (128 * 1024)})
    status = producer.snapshot()
    assert status["observed"] == status["dropped"] == status["oversized"] == 1
    assert status["enqueued"] == 0 and frames.empty()
    assert status["reason"] == "chunk_limit"


def test_closed_queue_finishes_only_preview():
    producer, _ = capture(queue=SimpleNamespace(put_nowait=Mock(side_effect=ValueError("closed"))))
    producer.offer(chunk(), IDENTITY)
    assert producer.snapshot()["reason"] == "preview_error"
    assert producer.snapshot()["dropped"] == 1


def test_extended_precision_is_rejected_without_silently_narrowing_values():
    if np.dtype(np.longdouble).itemsize <= 8:
        pytest.skip("This platform aliases longdouble to float64")
    producer, frames = capture()
    producer.offer(chunk(dtype=np.longdouble), IDENTITY)
    assert producer.snapshot()["reason"] == "unsupported_dtype"
    assert frames.empty()


class Source:
    is_connected = True
    is_streaming = True

    def __init__(self, reads=3):
        self.reads = reads

    async def read_chunk(self):
        if not self.reads:
            return None
        self.reads -= 1
        return chunk().raw_data

    async def start_stream(self):
        self.is_streaming = True

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False


def runtime(source=None, frames=None):
    events, main = [], Queue()
    acquisition = AcquisitionRuntime(
        source or Source(), Queue(), DataManager(main), SimpleNamespace(put_event=events.append),
        source_id="source", preparation_required=False, generation="run", config_revision="revision",
        preview_queue=Queue() if frames is None else frames,
    )
    acquisition.connection_id = "connection"
    return acquisition, events, main


async def start(acquisition, **changes):
    payload = {"capture_id": "capture", "generation": "run", "expected_revision": "revision",
               "connection_id": "connection", "selection": {}}
    await acquisition.execute((AcquisitionCommand.start_preview, CommandRequest("start", {**payload, **changes})))


@pytest.mark.asyncio
async def test_runtime_controls_are_correlated_and_preview_errors_do_not_break_main_flow():
    acquisition, events, main = runtime()
    await start(acquisition)
    assert events[-1].data.command == "StartAcquisitionPreview" and events[-1].data.ok
    acquisition.preview.offer = Mock(side_effect=RuntimeError("preview failure"))
    await acquisition.fetch()
    assert acquisition._fetch_failure is None
    assert acquisition.stream_chunk_count == 3 and main.qsize() == 3
    assert acquisition.stream_sample_count == 15 and acquisition._last_sequence == 2
    await acquisition.execute((AcquisitionCommand.get_preview, CommandRequest("get", {"generation": "run"})))
    status = events[-1].data.data
    assert events[-1].data.command == "GetAcquisitionPreview"
    assert status["stream_chunk_count"] == 3 and status["last_sequence"] == 2


@pytest.mark.asyncio
async def test_active_check_failure_does_not_break_main_flow(monkeypatch):
    acquisition, _, main = runtime()
    await start(acquisition)
    def failed(_self):
        raise RuntimeError("preview clock failure")
    monkeypatch.setattr(PreviewCapture, "active", property(failed))
    await acquisition.fetch()
    assert acquisition._fetch_failure is None and main.qsize() == 3
    assert acquisition.preview.snapshot()["reason"] == "preview_error"


@pytest.mark.asyncio
async def test_disabled_preview_does_not_copy_encode_or_describe_samples(monkeypatch):
    class NoCopy(np.ndarray):
        def copy(self, *args, **kwargs):
            raise AssertionError("Disabled preview must not copy arrays")
    acquisition, _, main = runtime()
    source = acquisition.data_source
    async def read():
        if not source.reads:
            return None
        source.reads -= 1
        return chunk().raw_data.view(NoCopy)
    source.read_chunk = read
    acquisition._descriptor = SimpleNamespace(to_dict=Mock(side_effect=AssertionError("metadata copy")))
    encode = Mock(side_effect=AssertionError("pickle copy"))
    monkeypatch.setattr("omnibci.infrastructure.acquisition.preview_capture.pickle.dumps", encode)
    await acquisition.fetch()
    assert acquisition._fetch_failure is None and main.qsize() == 3
    acquisition._descriptor.to_dict.assert_not_called()
    encode.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"generation": "old"}, {"expected_revision": "old"},
                                     {"connection_id": "old"}])
async def test_start_rejects_stale_runtime_identity(changes):
    acquisition, events, _ = runtime()
    await start(acquisition, **changes)
    assert not events[-1].data.ok
    assert acquisition.preview.snapshot()["status"] == "idle"


@pytest.mark.asyncio
async def test_start_requires_streaming_but_completed_capture_is_still_queryable():
    acquisition, events, _ = runtime()
    acquisition.data_source.is_streaming = False
    await start(acquisition)
    assert not events[-1].data.ok
    acquisition.data_source.is_streaming = True
    await start(acquisition)
    await acquisition._on_stop_stream()
    await acquisition.execute((AcquisitionCommand.get_preview, CommandRequest("get", {"generation": "run"})))
    assert events[-1].data.ok and events[-1].data.data["status"] == "finished"


@pytest.mark.asyncio
async def test_main_loop_expires_capture_when_source_does_not_produce_data():
    acquisition, _, _ = runtime()
    now = [1.0]
    acquisition.preview = PreviewCapture(Queue(), clock=lambda: now[0])
    await start(acquisition)
    now[0] = 12.0
    acquisition.command_queue.put(AcquisitionCommand.stop_acquisition)
    await acquisition.main_loop()
    assert acquisition.preview.snapshot()["reason"] == "timeout"


@pytest.mark.asyncio
async def test_chunk_whose_read_precedes_arm_is_captured_if_context_is_unchanged():
    entered, release = asyncio.Event(), asyncio.Event()
    source = Source(reads=1)
    async def read():
        if not source.reads:
            return None
        source.reads -= 1
        entered.set()
        await release.wait()
        return chunk().raw_data
    source.read_chunk = read
    frames = Queue()
    acquisition, _, main = runtime(source, frames)
    task = asyncio.create_task(acquisition.fetch())
    await entered.wait()
    await start(acquisition)
    release.set()
    await task
    assert main.qsize() == frames.qsize() == 1


@pytest.mark.asyncio
async def test_revision_change_ends_capture_before_pending_read_can_be_mislabeled():
    entered, release = asyncio.Event(), asyncio.Event()
    source = Source(reads=1)
    async def read():
        if not source.reads:
            return None
        source.reads -= 1
        entered.set()
        await release.wait()
        return chunk().raw_data
    source.read_chunk = read
    frames = Queue()
    acquisition, _, main = runtime(source, frames)
    await start(acquisition)
    task = asyncio.create_task(acquisition.fetch())
    await entered.wait()
    await acquisition.execute((AcquisitionCommand.update_revision, CommandRequest("revision", {
        "generation": "run", "expected_revision": "revision", "target_revision": "a" * 64,
    })))
    release.set()
    await task
    assert main.qsize() == 1 and frames.empty()
    assert acquisition.preview.snapshot()["reason"] == "context_changed"


def test_continuous_capture_survives_chunk_time_and_cumulative_byte_limits():
    now, frames = [0.0], Queue()
    producer = PreviewCapture(frames, clock=lambda: now[0])
    producer.start("continuous", None, IDENTITY, continuous=True)
    raw = np.arange(17 * 600, dtype=np.float64).reshape(17, 600)
    for sequence in range(70):
        now[0] += 1
        producer.offer(SignalChunk(sequence, sequence, raw), IDENTITY)
        frame = pickle.loads(frames.get_nowait())
        assert frame["row_indices"] == list(range(MAX_ROWS + 1))
        assert frame["sample_count"] == min(MAX_SAMPLES, MAX_VALUES // 17)
        assert frame["values"].size <= MAX_VALUES
    status = producer.snapshot()
    assert status["continuous"] is True and status["status"] == "capturing"
    assert status["observed"] == status["enqueued"] == 70
    assert status["enqueued_bytes"] > MAX_CACHE_BYTES
    assert status["reason"] is None and status["dropped"] == 0


@pytest.mark.parametrize("rows", [2, 3, 20])
def test_automatic_selection_preserves_small_matrices_and_original_trigger_row(rows):
    frames = Queue()
    producer = PreviewCapture(frames)
    producer.start("automatic", None, IDENTITY, continuous=True)
    assert producer.snapshot()["selection"] is None
    raw = np.arange(rows * 5, dtype=np.int32).reshape(rows, 5)
    expected_rows = list(range(min(MAX_ROWS, rows - 1))) + [rows - 1]
    expected = raw[expected_rows].copy()
    producer.offer(SignalChunk(123, 0, raw), IDENTITY)
    raw[:] = -1
    frame = pickle.loads(frames.get_nowait())
    assert frame["row_indices"] == expected_rows
    assert frame["sample_count"] == 5
    np.testing.assert_array_equal(frame["values"], expected)


def test_full_continuous_channel_drops_frames_without_ending_capture():
    frames = Queue(maxsize=1)
    producer = PreviewCapture(frames)
    producer.start("continuous", None, IDENTITY, continuous=True)
    for sequence in range(40):
        producer.offer(chunk(sequence), IDENTITY)
    status = producer.snapshot()
    assert (status["observed"], status["enqueued"], status["dropped"]) == (40, 1, 39)
    assert status["status"] == "capturing"


def test_continuous_capture_can_be_replaced_but_finite_capture_cannot():
    producer = PreviewCapture(Queue())
    producer.start("automatic", None, IDENTITY, continuous=True)
    producer.start("selected", PreviewSelection(rows=(1,)), IDENTITY, continuous=True)
    assert producer.snapshot()["capture_id"] == "selected"
    producer.start("finite", PreviewSelection(chunk_count=1), IDENTITY)
    assert producer.snapshot()["continuous"] is False
    with pytest.raises(ValueError, match="active"):
        producer.start("automatic", None, IDENTITY, continuous=True)
    with pytest.raises(ValueError, match="capture"):
        producer.stop("selected")
    producer.offer(chunk(), IDENTITY)
    assert producer.snapshot()["reason"] == "chunk_limit"


@pytest.mark.parametrize("continuous", [None, 0, 1, "true"])
def test_continuous_requires_a_boolean(continuous):
    producer = PreviewCapture(Queue())
    with pytest.raises(ValueError, match="continuous"):
        producer.start("invalid", PreviewSelection(), IDENTITY, continuous=continuous)
    assert producer.snapshot()["status"] == "idle"


def test_automatic_selection_requires_continuous_mode():
    producer = PreviewCapture(Queue())
    with pytest.raises(ValueError, match="selection"):
        producer.start("invalid", None, IDENTITY)


@pytest.mark.asyncio
async def test_each_stream_automatically_captures_its_first_chunk_and_retains_eof_result():
    source, frames = Source(), Queue()
    source.is_streaming = False
    acquisition, _, main = runtime(source, frames)
    await acquisition._on_start_stream()
    first_capture = acquisition.preview.snapshot()["capture_id"]
    assert first_capture and acquisition.preview.snapshot()["continuous"] is True
    await acquisition.fetch_task
    assert main.qsize() == frames.qsize() == 3
    assert pickle.loads(frames.get_nowait())["sequence_id"] == 0
    assert acquisition.preview.snapshot()["reason"] == "source_eof"
    assert acquisition.preview.snapshot()["capture_id"] == first_capture
    source.reads = 2
    await acquisition._on_start_stream()
    second_capture = acquisition.preview.snapshot()["capture_id"]
    assert second_capture != first_capture
    await acquisition.fetch_task
    assert main.qsize() == 5 and acquisition.preview.snapshot()["observed"] == 2
    remaining = [pickle.loads(frames.get_nowait()) for _ in range(frames.qsize())]
    assert [item["sequence_id"] for item in remaining if item["capture_id"] == second_capture] == [3, 4]


@pytest.mark.asyncio
async def test_automatic_preview_start_failure_does_not_prevent_main_stream():
    source = Source()
    source.is_streaming = False
    acquisition, _, main = runtime(source)
    acquisition.preview.start = Mock(side_effect=RuntimeError("preview failure"))
    await acquisition._on_start_stream()
    await acquisition.fetch_task
    assert main.qsize() == 3 and acquisition._fetch_failure is None
    acquisition.preview.start.assert_called_once()


@pytest.mark.asyncio
async def test_continuous_flag_is_forwarded_and_strictly_validated_by_runtime():
    acquisition, events, _ = runtime()
    await start(acquisition, continuous=1)
    assert not events[-1].data.ok
    await start(acquisition, continuous=True)
    assert events[-1].data.ok and events[-1].data.data["continuous"] is True


@pytest.mark.asyncio
async def test_stopping_stream_retains_its_automatic_capture():
    waiting = asyncio.Event()
    source, frames = Source(reads=1), Queue()
    source.is_streaming = False

    async def read():
        if source.reads:
            source.reads -= 1
            return chunk().raw_data
        waiting.set()
        await asyncio.Event().wait()

    source.read_chunk = read
    acquisition, _, main = runtime(source, frames)
    await acquisition._on_start_stream()
    await waiting.wait()
    capture_id = acquisition.preview.snapshot()["capture_id"]
    await acquisition._on_start_stream()
    assert acquisition.preview.snapshot()["capture_id"] == capture_id
    await acquisition._on_stop_stream()
    status = acquisition.preview.snapshot()
    assert status["capture_id"] == capture_id and status["reason"] == "source_stopped"
    assert status["status"] == "finished" and status["observed"] == 1
    assert main.qsize() == frames.qsize() == 1
    assert pickle.loads(frames.get_nowait())["capture_id"] == capture_id


@pytest.mark.asyncio
async def test_start_without_preview_channel_preserves_main_stream():
    source = Source()
    source.is_streaming = False
    acquisition, _, main = runtime(source)
    acquisition.preview = PreviewCapture(None)
    acquisition.preview.start = Mock(side_effect=AssertionError("No preview channel"))
    await acquisition._on_start_stream()
    await acquisition.fetch_task
    assert main.qsize() == 3 and acquisition._fetch_failure is None
    acquisition.preview.start.assert_not_called()


def test_continuous_capture_still_drops_oversized_frames_and_ignores_stale_identity():
    frames = Queue()
    producer = PreviewCapture(frames)
    producer.start("continuous", None, IDENTITY, continuous=True)
    producer.offer(chunk(), {**IDENTITY, "connection_id": "old"})
    assert producer.snapshot()["observed"] == 0
    producer.offer(chunk(), IDENTITY, descriptor={"description": "x" * (128 * 1024)})
    status = producer.snapshot()
    assert status["status"] == "capturing"
    assert status["observed"] == status["dropped"] == status["oversized"] == 1
    assert frames.empty()
