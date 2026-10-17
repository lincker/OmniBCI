"""Queue timing preserves data payloads, delivery order and capacity semantics."""

import logging
import multiprocessing as mp
from queue import Full, Queue

import numpy as np
import pytest

from omnibci.application.events import StreamBarrier
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc import data_manager as module
from omnibci.infrastructure.ipc.data_manager import DataManager


def _chunk(sequence=17):
    return SignalChunk(1_700_000_000_000_000_000, sequence,
                       np.array([[1., 2., 3.], [7., 0., 0.]], dtype=np.float32))


def _barrier():
    return StreamBarrier("drain-17", "run", "r1", 17, "session")


def _timing_records(caplog):
    return [record for record in caplog.records if record.name == module.__name__]


@pytest.mark.parametrize("timeout", [None, 0.1], ids=["blocking", "timed"])
@pytest.mark.parametrize("kind", ["chunk", "barrier"])
def test_queue_dwell_uses_monotonic_time_and_preserves_payload(monkeypatch, caplog, timeout, kind):
    clock = iter([2_000_000_000, 2_012_500_000])
    monkeypatch.setattr(module, "monotonic_ns", lambda: next(clock), raising=False)
    manager = DataManager(buffer=Queue())
    item = _chunk() if kind == "chunk" else _barrier()

    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        manager.put_data(item)
        received = manager.get_data(timeout=timeout)

    assert received is item
    record, = _timing_records(caplog)
    assert record.levelno == logging.DEBUG
    assert record.queue_dwell_ms == 12.5
    assert isinstance(record.queue_dwell_ms, float)
    assert record.item_type == type(item).__name__
    assert record.sequence_id == (17 if kind == "chunk" else None)
    assert record.command_id == (None if kind == "chunk" else "drain-17")


@pytest.mark.parametrize("timeout", [None, 0.1], ids=["blocking", "timed"])
def test_raw_queue_items_are_delivered_without_timing(caplog, timeout):
    buffer = Queue()
    manager = DataManager(buffer=buffer)
    items = [_chunk(), _barrier()]
    for item in items:
        buffer.put_nowait(item)

    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        received = [manager.get_data(timeout=timeout) for _ in items]

    assert all(actual is expected for actual, expected in zip(received, items, strict=True))
    assert not _timing_records(caplog)


def test_timeout_returns_none_without_timing_record(caplog):
    manager = DataManager(buffer=Queue())
    with caplog.at_level(logging.DEBUG, logger=module.__name__):
        assert manager.get_data(timeout=0.001) is None
    assert not _timing_records(caplog)


def test_full_queue_propagates_without_discarding_existing_item():
    manager = DataManager(buffer=Queue(maxsize=1))
    first = _chunk()
    manager.put_data(first)

    with pytest.raises(Full):
        manager.put_data(_barrier())

    assert manager.get_data(timeout=0.1) is first
    assert manager.get_data(timeout=0.001) is None


def _produce_data(manager, items):
    for item in items:
        manager.put_data(item)


@pytest.mark.integration
def test_spawned_producer_preserves_chunk_then_barrier_payloads_and_fifo(caplog):
    context = mp.get_context("spawn")
    buffer = context.Queue()
    manager = DataManager(buffer=buffer)
    expected_chunk, expected_barrier = _chunk(), _barrier()
    process = context.Process(target=_produce_data, args=(manager, (expected_chunk, expected_barrier)))
    try:
        process.start()
        with caplog.at_level(logging.DEBUG, logger=module.__name__):
            chunk = manager.get_data(timeout=10)
            barrier = manager.get_data(timeout=10)
        process.join(timeout=5)
        assert process.exitcode == 0
        assert isinstance(chunk, SignalChunk)
        assert chunk.timestamp == expected_chunk.timestamp
        assert chunk.sequence_id == expected_chunk.sequence_id
        assert chunk.raw_data.dtype == expected_chunk.raw_data.dtype
        np.testing.assert_array_equal(chunk.raw_data, expected_chunk.raw_data)
        assert barrier == expected_barrier
        records = _timing_records(caplog)
        assert [record.item_type for record in records] == ["SignalChunk", "StreamBarrier"]
        assert all(record.queue_dwell_ms >= 0 for record in records)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
        buffer.close()
        buffer.join_thread()
