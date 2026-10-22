"""Chunk statistics describe successful producer delivery, including short chunks."""
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime


def runtime(chunks, sink=None):
    source = SimpleNamespace(
        is_connected=True, is_streaming=True,
        read_chunk=AsyncMock(side_effect=[*chunks, None]),
        stop_stream=AsyncMock(),
    )
    return AcquisitionRuntime(source, Queue(), sink or SimpleNamespace(put_data=lambda _: None),
                              source_id="test", preparation_required=False)


@pytest.mark.asyncio
async def test_stream_counts_reset_but_chunk_sequence_continues():
    acq = runtime([np.zeros((3, 4)), np.zeros((3, 1))])
    await acq.fetch()
    assert (acq.stream_chunk_count, acq.stream_sample_count, acq._last_sequence) == (2, 5, 1)
    acq.data_source.read_chunk = AsyncMock(side_effect=[np.zeros((3, 2)), None])
    await acq.fetch()
    assert (acq.stream_chunk_count, acq.stream_sample_count, acq._last_sequence) == (1, 2, 2)
    fresh = runtime([])
    assert (fresh.stream_chunk_count, fresh.stream_sample_count, fresh._last_sequence) == (0, 0, -1)


@pytest.mark.asyncio
async def test_rejected_main_delivery_does_not_increment_counts():
    def reject(_):
        raise RuntimeError("main queue rejected the chunk")
    acq = runtime([np.zeros((2, 7))], SimpleNamespace(put_data=reject))
    await acq.fetch()
    assert (acq.stream_chunk_count, acq.stream_sample_count, acq._last_sequence) == (0, 0, -1)
    assert "main queue rejected" in acq._fetch_failure
