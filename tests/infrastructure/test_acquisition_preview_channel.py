"""Preview transport keeps memory and waiting bounded without a feeder thread."""
from queue import Empty, Full
from time import monotonic

import pytest

from omnibci.application.acquisition_preview import MAX_FRAME_BYTES, PREVIEW_QUEUE_SIZE
from omnibci.infrastructure.acquisition.preview_channel import PreviewChannel


@pytest.fixture
def channel():
    output = PreviewChannel()
    yield output
    output.close()


def test_fixed_capacity_and_fifo_survive_slot_reuse(channel):
    for sequence in range(PREVIEW_QUEUE_SIZE):
        channel.put_nowait(bytes([sequence]) * MAX_FRAME_BYTES)
    with pytest.raises(Full):
        channel.put_nowait(b"overflow")
    first = channel.get(False)
    channel.put_nowait(b"reused")
    for sequence in range(1, PREVIEW_QUEUE_SIZE):
        assert channel.get(False) == bytes([sequence]) * MAX_FRAME_BYTES
    assert channel.get(False) == b"reused"
    assert first == bytes(MAX_FRAME_BYTES)
    with pytest.raises(Empty):
        channel.get(False)


def test_contended_lock_never_blocks_writer_or_reader(channel):
    channel.put_nowait(b"queued")
    channel._lock.acquire()
    try:
        started = monotonic()
        with pytest.raises(Full):
            channel.put_nowait(b"dropped")
        with pytest.raises(Empty):
            channel.get(False)
        with pytest.raises(Empty):
            channel.get(timeout=0.01)
        assert monotonic() - started < 0.5
    finally:
        channel._lock.release()
    assert channel.get(False) == b"queued"


@pytest.mark.parametrize("timeout", [None, 0, 0.01])
def test_empty_reads_have_a_finite_deadline(channel, timeout):
    started = monotonic()
    with pytest.raises(Empty):
        channel.get(timeout=timeout)
    elapsed = monotonic() - started
    assert (0.05 if timeout is None else timeout) <= elapsed < 0.5


def test_empty_bytes_are_a_frame_and_do_not_mean_empty_channel(channel):
    channel.put_nowait(b"")
    assert channel.get(False) == b""
    with pytest.raises(Empty):
        channel.get(False)


def test_expired_poll_does_not_read_a_frame_that_arrives_after_deadline(channel, monkeypatch):
    now = [1.0]

    def resume_after_deadline(delay):
        assert 0 < delay <= 0.005
        now[0] = 2.0
        channel.put_nowait(b"late")

    monkeypatch.setattr("omnibci.infrastructure.acquisition.preview_channel.monotonic", lambda: now[0])
    monkeypatch.setattr("omnibci.infrastructure.acquisition.preview_channel.sleep", resume_after_deadline)
    with pytest.raises(Empty):
        channel.get(timeout=0.05)
    assert channel.get(False) == b"late"


@pytest.mark.parametrize("payload", [None, "text", bytearray(b"mutable"), memoryview(b"view")])
def test_only_encoded_bytes_are_accepted(channel, payload):
    with pytest.raises(TypeError):
        channel.put_nowait(payload)


def test_oversize_payload_does_not_consume_a_slot(channel):
    with pytest.raises(ValueError, match="frame"):
        channel.put_nowait(b"x" * (MAX_FRAME_BYTES + 1))
    with pytest.raises(Empty):
        channel.get(False)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_unbounded_or_negative_timeouts_are_rejected(channel, timeout):
    with pytest.raises(ValueError, match="timeout"):
        channel.get(timeout=timeout)


def test_close_is_local_idempotent_and_does_not_wait_for_the_lock(channel):
    channel._lock.acquire()
    try:
        channel.close()
        channel.close()
        with pytest.raises(ValueError, match="closed"):
            channel.put_nowait(b"closed")
        with pytest.raises(ValueError, match="closed"):
            channel.get()
    finally:
        channel._lock.release()


def _cache_frame(sequence=0, capture="a", generation="g", values=None):
    import pickle
    import numpy as np

    return pickle.dumps(dict(
        capture_id=capture, generation=generation, sequence_id=sequence,
        timestamp_ns="1800000000000000001", original_shape=[2, 2],
        dtype="float64", row_indices=[0, 1], sample_start=0, sample_count=2,
        values=np.array([[1, 2], [0, 1]]) if values is None else values,
        descriptor=None, connection_id="c", source_id="s", acquisition_revision="r", engine_revision="engine",
        trigger_row_index=1,
    ))


def test_cache_is_bounded_and_filters_captures_and_generations():
    from queue import Queue
    from omnibci.application.acquisition_preview import MAX_CHUNKS
    from omnibci.infrastructure.acquisition.preview_cache import PreviewCache

    cache = PreviewCache(Queue(), generation="g")
    for i in range(MAX_CHUNKS + 2):
        cache.accept(_cache_frame(i))
    assert len(cache.read("a")["chunks"]) == MAX_CHUNKS
    assert cache.read("a", 0)["chunk"] is None
    cache.accept(_cache_frame(99, generation="old"))
    assert cache.read("a", 99)["chunk"] is None
    cache.accept(_cache_frame(100, capture="b"))
    assert [item["sequence_id"] for item in cache.read("b")["chunks"]] == [100]
    assert cache.read("a", 100)["chunk"] is None
    with pytest.raises(ValueError):
        cache.accept(b"x" * (MAX_FRAME_BYTES + 1))


def test_details_preserve_numbers_and_cannot_mutate_cached_values():
    from queue import Queue
    import numpy as np
    from omnibci.infrastructure.acquisition.preview_cache import PreviewCache

    cache = PreviewCache(Queue(), generation="g")
    cache.accept(_cache_frame(values=np.array([[float("nan"), float("inf")], [-float("inf"), 0.25]])))
    detail = cache.read("a", 0)["chunk"]
    assert detail["values"] == [["NaN", "Infinity"], ["-Infinity", 0.25]]
    detail["values"][0][0] = 42
    assert cache.read("a", 0)["chunk"]["values"][0][0] == "NaN"
    cache.accept(_cache_frame(1, values=np.array([[2**60, -(2**60)], [0, 1]], dtype=np.int64)))
    assert cache.read("a", 1)["chunk"]["values"][0] == [str(2**60), str(-(2**60))]


@pytest.mark.asyncio
async def test_consumer_stops_with_empty_queue_and_isolates_malformed_frame():
    import asyncio
    from queue import Queue
    from omnibci.infrastructure.acquisition.preview_cache import PreviewCache

    queue = Queue()
    cache = PreviewCache(queue, generation="g")
    await cache.start()
    queue.put(b"not a frame")
    queue.put(_cache_frame())
    for _ in range(100):
        if cache.read("a")["chunks"]:
            break
        await asyncio.sleep(0.01)
    assert len(cache.read("a")["chunks"]) == 1
    assert cache.read("a")["cache_errors"] == 1
    await asyncio.wait_for(cache.stop(), 1)
    await cache.stop()
    assert cache.stopped
