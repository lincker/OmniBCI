"""Preview IPC must neither hold children open nor steal processing data."""
import asyncio
import multiprocessing as mp
import pickle
from queue import Empty, Full, Queue
import time
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from omnibci.application.acquisition_preview import MAX_FRAME_BYTES, PREVIEW_QUEUE_SIZE
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
from tests.neuroengine.test_neuro_engine import _make_engine


def _frame(sequence=0):
    return pickle.dumps({
        "capture_id": "capture", "generation": "generation", "sequence_id": sequence,
        "timestamp_ns": "1800000000000000001", "original_shape": [2, 2],
        "dtype": "int64", "row_indices": [0, 1], "sample_start": 0, "sample_count": 2,
        "trigger_row_index": 1, "values": np.array([[11, 12], [0, 7]], dtype=np.int64),
        "descriptor": None, "connection_id": "connection", "source_id": "source",
        "config_revision": "revision",
    })


def _publish_frame(output, payload):
    output.put_nowait(payload)
    output.close()
    output.join_thread()


def _run_worker_with_unconsumed_preview(output, report):
    from omnibci.infrastructure.acquisition.worker import AcquisitionWorker

    worker = AcquisitionWorker(command_queue=None, settings_snapshot={}, preview_queue=output)
    worker._load_data_source = lambda: None

    def fill_preview_queue():
        payload = pickle.dumps({"data": b"x" * (MAX_FRAME_BYTES - 1024)})
        for _ in range(PREVIEW_QUEUE_SIZE):
            output.put_nowait(payload)
        try:
            output.put_nowait(payload)
        except Full:
            report.send((PREVIEW_QUEUE_SIZE, len(payload), "full"))
        else:
            raise AssertionError("Preview queue did not enforce its capacity")

    worker._run = fill_preview_queue
    try:
        worker.run()
    finally:
        report.close()


def _close_process(process):
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    process.close()


async def _wait_for_frame(cache, sequence=0):
    async with asyncio.timeout(10):
        while cache.read("capture", sequence)["chunk"] is None:
            await asyncio.sleep(0.01)


@pytest.mark.integration
def test_spawned_worker_exits_with_a_full_preview_queue_and_no_consumer():
    context = mp.get_context("spawn")
    output = context.Queue(PREVIEW_QUEUE_SIZE)
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_run_worker_with_unconsumed_preview, args=(output, sender))
    try:
        process.start()
        sender.close()
        assert receiver.poll(15), "Child did not finish offering bounded preview frames"
        count, encoded_bytes, state = receiver.recv()
        assert count == PREVIEW_QUEUE_SIZE and state == "full"
        assert 64 * 1024 < encoded_bytes <= MAX_FRAME_BYTES
        process.join(timeout=5)
        assert not process.is_alive(), "Child waited for an unconsumed preview feeder"
        assert process.exitcode == 0
    finally:
        _close_process(process)
        receiver.close()
        sender.close()
        output.cancel_join_thread()
        output.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_spawned_bytes_reach_preview_consumer_and_empty_consumer_stops():
    context = mp.get_context("spawn")
    output = context.Queue(PREVIEW_QUEUE_SIZE)
    cache = PreviewCache(output, generation="generation")
    process = context.Process(target=_publish_frame, args=(output, _frame()))
    try:
        await cache.start()
        process.start()
        await _wait_for_frame(cache)
        await asyncio.to_thread(process.join, 5)
        assert process.exitcode == 0
        snapshot = cache.read("capture", 0)
        assert snapshot["available"] is True
        assert snapshot["chunk"]["values"] == [[11, 12], [0, 7]]
        assert snapshot["chunk"]["timestamp_ns"] == "1800000000000000001"
        await asyncio.wait_for(cache.stop(), timeout=1)
        await cache.stop()
        assert cache.stopped and cache.read("capture")["available"] is False
    finally:
        _close_process(process)
        await cache.stop()
        output.cancel_join_thread()
        output.close()


@pytest.mark.asyncio
async def test_preview_consumer_keeps_processing_final_frames_during_both_worker_joins():
    output = Queue(PREVIEW_QUEUE_SIZE)
    cache = PreviewCache(output, generation="generation")
    engine = _make_engine(preview_cache=cache)

    def join_with_last_frame(sequence, timeout):
        assert cache.read("capture")["available"] is True
        output.put_nowait(_frame(sequence))
        deadline = time.monotonic() + 1
        while cache.read("capture", sequence)["chunk"] is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert cache.read("capture", sequence)["chunk"] is not None

    for sequence, worker in enumerate((engine.acquisition_worker, engine.signal_processing_worker)):
        worker.is_alive.return_value = False
        worker.join.side_effect = lambda timeout, sequence=sequence: join_with_last_frame(sequence, timeout)
    try:
        await engine._start()
        await asyncio.wait_for(engine._stop(), timeout=3)
        assert engine.shutdown_complete and cache.stopped
        assert [item["sequence_id"] for item in cache.read("capture")["chunks"]] == [0, 1]
    finally:
        await cache.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["acquisition_start", "gateway_start"])
async def test_engine_start_failure_stops_preview_and_closes_its_owned_queue(failure):
    output = mp.get_context("spawn").Queue(PREVIEW_QUEUE_SIZE)
    cache = PreviewCache(output, generation="generation")
    engine = _make_engine(preview_cache=cache, owned_ipc_queues=(output,))
    engine.acquisition_worker.is_alive.return_value = False
    engine.signal_processing_worker.is_alive.return_value = False
    failing_start = (engine.acquisition_worker.start if failure == "acquisition_start"
                     else engine.peer_gateway.start)
    failing_start.side_effect = RuntimeError("startup failed")
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            await engine._start()
        assert engine.shutdown_complete and cache.stopped
        assert cache.read("capture")["available"] is False
        with pytest.raises(ValueError, match="closed"):
            output.put_nowait(_frame())
        if failure == "gateway_start":
            engine.event_bus.stop.assert_awaited_once()
            engine.peer_gateway.stop.assert_awaited_once()
    finally:
        await cache.stop()
        output.cancel_join_thread()
        output.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer_stopped", [False, True])
async def test_preview_stop_failure_continues_cleanup_but_preserves_active_consumer_queues(consumer_stopped):
    cache = MagicMock()
    cache.start = AsyncMock()
    cache.stop = AsyncMock(side_effect=RuntimeError("preview consumer failed"))
    cache.stopped = consumer_stopped
    output = MagicMock()
    engine = _make_engine(preview_cache=cache, owned_ipc_queues=(output,))
    engine.acquisition_worker.is_alive.return_value = False
    engine.signal_processing_worker.is_alive.return_value = False
    await engine._start()

    with pytest.raises(ExceptionGroup, match="shutdown failed"):
        await engine._stop()

    engine.peer_gateway.stop.assert_awaited_once()
    engine.event_bus.stop.assert_awaited_once()
    if consumer_stopped:
        output.close.assert_called_once()
        engine.acquisition_command_queue.close.assert_called_once()
        engine.processor_command_queue.close.assert_called_once()
    else:
        assert not engine.shutdown_complete
        output.close.assert_not_called()
        engine.acquisition_command_queue.close.assert_not_called()
        engine.processor_command_queue.close.assert_not_called()


@pytest.mark.asyncio
async def test_desktop_retained_worker_keeps_preview_queue_owned_until_worker_exits():
    output = mp.get_context("spawn").Queue(PREVIEW_QUEUE_SIZE)
    cache = PreviewCache(output, generation="generation")
    engine = _make_engine(preview_cache=cache, owned_ipc_queues=(output,),
                          allow_process_termination=False)
    engine.acquisition_worker.is_alive.return_value = True
    engine.signal_processing_worker.is_alive.return_value = False
    try:
        await engine._start()
        with pytest.raises(ExceptionGroup, match="shutdown failed"):
            await engine._stop()
        assert not engine.shutdown_complete
        engine.acquisition_worker.terminate.assert_not_called()
        engine.acquisition_worker.kill.assert_not_called()
        output.put_nowait(_frame())
        engine.acquisition_worker.is_alive.return_value = False
        await engine._stop()
        assert engine.shutdown_complete
        with pytest.raises(ValueError, match="closed"):
            output.put_nowait(_frame(1))
    finally:
        engine.acquisition_worker.is_alive.return_value = False
        await engine._stop()
        await cache.stop()
        output.cancel_join_thread()
        output.close()


@pytest.mark.asyncio
async def test_builder_owns_one_independent_bounded_preview_queue_per_graph():
    engines = []
    try:
        for generation in ("first", "second"):
            engines.append(NeuroEngineBuilder(settings_snapshot={"CONFIG.generation": generation}).build())
        first, second = engines
        output = first.acquisition_worker.preview_queue
        other_output = second.acquisition_worker.preview_queue
        assert output is not other_output
        assert first.preview_cache._queue is output
        assert second.preview_cache._queue is other_output
        assert output is not first.data_manager.buffer
        assert output is not first.event_bus._event_queue
        assert output in first._owned_ipc_queues
        for _ in range(PREVIEW_QUEUE_SIZE):
            output.put_nowait(_frame())
        with pytest.raises(Full):
            output.put_nowait(_frame())
        with pytest.raises(Empty):
            other_output.get(timeout=0.05)
        first.data_manager.put_data("processing data")
        assert first.data_manager.get_data(timeout=1) == "processing data"
    finally:
        for engine in engines:
            await engine._stop()
            engine.acquisition_worker.close()
            engine.signal_processing_worker.close()
