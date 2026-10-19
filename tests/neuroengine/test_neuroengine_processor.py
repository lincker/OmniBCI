"""Tests for event-driven data delivery into the signal processor."""

import asyncio
import queue
import time
from unittest.mock import AsyncMock, MagicMock

from tests.processing_helpers import make_processor
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.signal_processing.signal_processor import (
    SignalProcessorCommand,
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
        sp = make_processor(detector=MagicMock(), decoder=MagicMock(),
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
        sp = make_processor(detector=MagicMock(), decoder=MagicMock(),
                             command_queue=MagicMock(), data_manager=dm,
                             event_bus=MagicMock(), auto_start_session=False)
        assert asyncio.run(sp.next_chunk(timeout=0.05)) is None


def test_stop_command_does_not_depend_on_queue_empty_probe():
    command_queue = queue.Queue()
    command_queue.put_nowait(SignalProcessorCommand.should_stop)
    command_queue.empty = MagicMock(return_value=True)
    processor = make_processor(
        detector=MagicMock(), decoder=MagicMock(), command_queue=command_queue,
        data_manager=MagicMock(), event_bus=MagicMock(), auto_start_session=False,
    )

    async def stop_after_data_read():
        processor._should_stop = True

    processor._current_state.run = AsyncMock(side_effect=stop_after_data_read)

    asyncio.run(processor.serve())

    processor._current_state.run.assert_not_awaited()
    command_queue.empty.assert_not_called()
