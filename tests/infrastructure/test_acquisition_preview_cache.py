"""The diagnostic cache is bounded, independent and safe for REST readers."""
import asyncio
import pickle
from queue import Queue

import numpy as np
import pytest

from omnibci.application.acquisition_preview import MAX_CHUNKS, MAX_FRAME_BYTES
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache


def frame(sequence=0, capture="a", generation="g", values=None):
    return pickle.dumps(dict(capture_id=capture, generation=generation, sequence_id=sequence,
                             timestamp_ns="1800000000000000001", original_shape=[2, 2],
                             dtype="float64", row_indices=[0, 1], sample_start=0, sample_count=2,
                             values=np.array([[1, 2], [0, 1]]) if values is None else values,
                             descriptor=None, connection_id="c", source_id="s", config_revision="r",
                             trigger_row_index=1))


def test_cache_is_bounded_and_filters_captures_and_generations():
    cache = PreviewCache(Queue(), generation="g")
    for i in range(MAX_CHUNKS + 2):
        cache.accept(frame(i))
    assert len(cache.read("a")["chunks"]) == MAX_CHUNKS
    assert cache.read("a", 0)["chunk"] is None
    cache.accept(frame(99, generation="old"))
    assert cache.read("a", 99)["chunk"] is None
    cache.accept(frame(100, capture="b"))
    assert [item["sequence_id"] for item in cache.read("b")["chunks"]] == [100]
    assert cache.read("a", 100)["chunk"] is None
    with pytest.raises(ValueError):
        cache.accept(b"x" * (MAX_FRAME_BYTES + 1))


def test_details_preserve_numbers_and_cannot_mutate_cached_values():
    cache = PreviewCache(Queue(), generation="g")
    cache.accept(frame(values=np.array([[float("nan"), float("inf")], [-float("inf"), 0.25]])))
    detail = cache.read("a", 0)["chunk"]
    assert detail["values"] == [["NaN", "Infinity"], ["-Infinity", 0.25]]
    detail["values"][0][0] = 42
    assert cache.read("a", 0)["chunk"]["values"][0][0] == "NaN"
    cache.accept(frame(1, values=np.array([[2**60, -(2**60)], [0, 1]], dtype=np.int64)))
    assert cache.read("a", 1)["chunk"]["values"][0] == [str(2**60), str(-(2**60))]


@pytest.mark.asyncio
async def test_consumer_stops_with_empty_queue_and_isolates_malformed_frame():
    queue = Queue()
    cache = PreviewCache(queue, generation="g")
    await cache.start()
    queue.put(b"not a frame")
    queue.put(frame())
    for _ in range(100):
        if cache.read("a")["chunks"]:
            break
        await asyncio.sleep(0.01)
    assert len(cache.read("a")["chunks"]) == 1
    assert cache.read("a")["cache_errors"] == 1
    await asyncio.wait_for(cache.stop(), 1)
    await cache.stop()
    assert cache.stopped
