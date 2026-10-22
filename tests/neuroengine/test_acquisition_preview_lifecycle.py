"""Preview IPC must neither hold children open nor steal processing data."""
import asyncio
import multiprocessing as mp
import os
from pathlib import Path
import pickle
from queue import Empty, Full
import subprocess
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from omnibci.application.acquisition_preview import MAX_FRAME_BYTES, PREVIEW_QUEUE_SIZE
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache
from omnibci.infrastructure.acquisition.preview_channel import PreviewChannel
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


def _put_frame_with_retry(output, payload):
    deadline = time.monotonic() + 1
    while True:
        try:
            output.put_nowait(payload)
            return
        except Full:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)


def _publish_frame(output, payload):
    _put_frame_with_retry(output, payload)
    output.close()


def _run_worker_with_unconsumed_preview(output, report):
    from omnibci.infrastructure.acquisition.worker import AcquisitionWorker

    worker = AcquisitionWorker(command_queue=None, settings_snapshot={}, preview_queue=output)
    worker._load_data_source = lambda: None

    def fill_preview_channel():
        payload = b"x" * MAX_FRAME_BYTES
        for _ in range(PREVIEW_QUEUE_SIZE):
            output.put_nowait(payload)
        try:
            output.put_nowait(payload)
        except Full:
            report.send((PREVIEW_QUEUE_SIZE, len(payload), "full"))
        else:
            raise AssertionError("Preview channel did not enforce its capacity")

    worker._run = fill_preview_channel
    try:
        worker.run()
    finally:
        report.close()


def _exit_with_preview_lock(output):
    output._lock.acquire()
    os._exit(0)


def _check_cache_after_locked_producer_exit():
    context = mp.get_context("spawn")
    output = PreviewChannel(context=context)
    process = context.Process(target=_exit_with_preview_lock, args=(output,))

    async def stop_cache():
        cache = PreviewCache(output, generation="generation")
        await cache.start()
        await asyncio.sleep(0.1)
        await asyncio.wait_for(cache.stop(), timeout=1)
        assert cache.stopped
        assert cache.read("capture")["available"] is False

    try:
        process.start()
        process.join(timeout=10)
        assert process.exitcode == 0
        assert not output._lock.acquire(False)
        asyncio.run(stop_cache())
        print("preview_cache_and_event_loop_stopped", flush=True)
    finally:
        _close_process(process)
        output.close()


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
def test_spawned_worker_exits_with_full_preview_channel_and_leaves_complete_maximum_frames():
    context = mp.get_context("spawn")
    output = PreviewChannel(context=context)
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_run_worker_with_unconsumed_preview, args=(output, sender))
    try:
        process.start()
        sender.close()
        assert receiver.poll(15), "Child did not finish offering bounded preview frames"
        count, encoded_bytes, state = receiver.recv()
        assert count == PREVIEW_QUEUE_SIZE and state == "full"
        assert encoded_bytes == MAX_FRAME_BYTES
        process.join(timeout=5)
        assert not process.is_alive(), "Child waited for an unconsumed preview channel"
        assert process.exitcode == 0
        for _ in range(PREVIEW_QUEUE_SIZE):
            assert output.get(timeout=0.5) == b"x" * MAX_FRAME_BYTES
        with pytest.raises(Empty):
            output.get(timeout=0.05)
    finally:
        _close_process(process)
        receiver.close()
        sender.close()
        output.close()


@pytest.mark.integration
def test_dead_producer_holding_preview_lock_does_not_block_cache_or_event_loop_shutdown():
    result = subprocess.run(
        [sys.executable, "-c", (
            "from tests.neuroengine.test_acquisition_preview_lifecycle "
            "import _check_cache_after_locked_producer_exit; "
            "_check_cache_after_locked_producer_exit()"
        )],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "preview_cache_and_event_loop_stopped" in result.stdout


@pytest.mark.integration
@pytest.mark.asyncio
async def test_spawned_bytes_reach_preview_consumer_and_empty_consumer_stops():
    context = mp.get_context("spawn")
    output = PreviewChannel(context=context)
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
        output.close()


@pytest.mark.asyncio
async def test_preview_consumer_keeps_processing_final_frames_during_both_worker_joins():
    output = PreviewChannel(context=mp.get_context("spawn"))
    cache = PreviewCache(output, generation="generation")
    engine = _make_engine(preview_cache=cache)

    def join_with_last_frame(sequence, timeout):
        assert cache.read("capture")["available"] is True
        _put_frame_with_retry(output, _frame(sequence))
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
        output.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["acquisition_start", "gateway_start"])
async def test_engine_start_failure_stops_preview_and_closes_its_owned_channel(failure):
    output = PreviewChannel(context=mp.get_context("spawn"))
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
        output.close()


@pytest.mark.asyncio
async def test_engine_rejects_restart_after_rollback_leaves_preview_consumer_running(monkeypatch):
    output = PreviewChannel(context=mp.get_context("spawn"))
    cache = PreviewCache(output, generation="generation")
    engine = _make_engine(preview_cache=cache, owned_ipc_queues=(output,))
    engine.acquisition_worker.start.side_effect = RuntimeError("worker startup failed")
    engine.acquisition_worker.is_alive.return_value = False
    engine.signal_processing_worker.is_alive.return_value = False
    stop = cache.stop
    monkeypatch.setattr(cache, "stop", AsyncMock(side_effect=RuntimeError("consumer shutdown failed")))
    try:
        with pytest.raises(RuntimeError, match="worker startup failed"):
            await engine._start()
        assert not cache.stopped and not engine.shutdown_complete
        engine.acquisition_worker.start.side_effect = None

        with pytest.raises(RuntimeError, match="partial lifecycle state"):
            await engine._start()

        engine.acquisition_worker.start.assert_called_once()
        engine.signal_processing_worker.start.assert_not_called()
    finally:
        monkeypatch.setattr(cache, "stop", stop)
        await engine._stop()
        await cache.stop()
        output.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer_stopped", [False, True])
async def test_preview_stop_failure_continues_cleanup_but_preserves_active_consumer_queues(consumer_stopped):
    cache = MagicMock()
    cache.start = AsyncMock()
    cache.stop = AsyncMock(side_effect=RuntimeError("preview consumer failed"))
    cache.stopped = True
    output = MagicMock()
    engine = _make_engine(preview_cache=cache, owned_ipc_queues=(output,))
    engine.acquisition_worker.is_alive.return_value = False
    engine.signal_processing_worker.is_alive.return_value = False
    await engine._start()
    cache.stopped = consumer_stopped

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
async def test_desktop_retained_worker_keeps_preview_channel_owned_until_worker_exits():
    output = PreviewChannel(context=mp.get_context("spawn"))
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
        output.close()


@pytest.mark.asyncio
async def test_builder_owns_one_independent_bounded_preview_channel_per_graph():
    engines = []
    try:
        for generation in ("first", "second"):
            engines.append(NeuroEngineBuilder(settings_snapshot={"CONFIG.generation": generation}).build())
        first, second = engines
        output = first.acquisition_worker.preview_queue
        other_output = second.acquisition_worker.preview_queue
        assert isinstance(output, PreviewChannel)
        assert isinstance(other_output, PreviewChannel)
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
