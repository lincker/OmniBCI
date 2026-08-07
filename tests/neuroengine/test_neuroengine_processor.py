"""Tests for event-driven data delivery into the signal processor."""

import asyncio
import queue
import time
from unittest.mock import MagicMock

from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.neuroengine.processing.signal_processor import (
    SignalProcessor,
)


class TestDataManagerTimeout:
    def test_returns_item_immediately_when_present(self):
        dm = DataManager(buffer=queue.Queue())
        dm.put_data("chunk")
        assert dm.get_data(timeout=0.5) == "chunk"

    def test_returns_none_after_timeout_when_empty(self):
        dm = DataManager(buffer=queue.Queue())
        t0 = time.perf_counter()
        assert dm.get_data(timeout=0.05) is None
        assert time.perf_counter() - t0 >= 0.04


class TestNextChunkEventDriven:
    def test_chunk_delivered_promptly(self):
        """数据到达后 next_chunk 立即返回（无轮询粒度延迟）。"""
        dm = DataManager(buffer=queue.Queue())
        sp = SignalProcessor(detector=MagicMock(), decoder=MagicMock(),
                             command_queue=MagicMock(), data_manager=dm,
                             event_bus=MagicMock(), auto_start_session=False)

        async def scenario():
            async def feeder():
                await asyncio.sleep(0.02)
                dm.put_data("the-chunk")
            asyncio.get_running_loop().create_task(feeder())
            t0 = time.perf_counter()
            chunk = await sp.next_chunk(timeout=1.0)
            return chunk, time.perf_counter() - t0

        chunk, elapsed = asyncio.run(scenario())
        assert chunk == "the-chunk"
        assert elapsed < 0.2

    def test_returns_none_when_no_data_within_window(self):
        dm = DataManager(buffer=queue.Queue())
        sp = SignalProcessor(detector=MagicMock(), decoder=MagicMock(),
                             command_queue=MagicMock(), data_manager=dm,
                             event_bus=MagicMock(), auto_start_session=False)
        assert asyncio.run(sp.next_chunk(timeout=0.05)) is None
