"""A dropped reader chunk must never become a successful drain/checkpoint."""
import asyncio
import queue

import numpy as np
import pytest

from omnibci.application.events import CommandRequest, CommandResult
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime, AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.shared.events import EventType


class Source:
    supports_checkpoint = True
    is_streaming = True
    is_connected = True

    def __init__(self, *, malformed_chunk=False):
        self.cursor = 0
        self.malformed_chunk = malformed_chunk

    async def read_chunk(self):
        self.cursor += 4
        return np.ones(4) if self.malformed_chunk else np.ones((2, 4))

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def start_stream(self):
        self.cursor = 0
        self.is_streaming = True

    def checkpoint(self):
        return {"cursor": self.cursor}


class Bus:
    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


@pytest.mark.parametrize("failure", ["queue_full", "malformed_chunk", "read"])
def test_terminal_fetch_failure_blocks_drain_checkpoint_and_manual_restart(failure):
    async def scenario():
        source, bus = Source(malformed_chunk=failure == "malformed_chunk"), Bus()
        data = DataManager(queue.Queue(maxsize=1))
        if failure == "queue_full":
            data.put_data("existing-chunk")
        acquisition = AcquisitionRuntime(
            source, queue.Queue(), data, bus,
            source_id="test-source", preparation_required=False,
            generation="generation", config_revision="revision",
        )
        if failure == "read":
            async def fail_read():
                raise OSError("device read failed")
            source.read_chunk = fail_read
        await acquisition.fetch()
        assert not source.is_streaming
        assert acquisition._last_sequence == -1
        if failure != "read":
            assert source.cursor == 4
        while data.get_data(0) is not None:
            pass
        for command, payload in (
            (AcquisitionCommand.stop_and_drain, {}),
            (AcquisitionCommand.get_checkpoint, {"generation": "generation", "expected_revision": "revision"}),
            (AcquisitionCommand.start_stream, None),
        ):
            await acquisition.execute((command, CommandRequest(command.name, payload)))
            result = bus.events[-1].data
            assert isinstance(result, CommandResult)
            assert not result.ok
            assert "lost stream continuity" in result.message
            assert data.get_data(0) is None
        # Reconnecting the same graph cannot erase evidence of the lost chunk.
        await acquisition.execute(AcquisitionCommand.disconnect)
        await acquisition.execute(AcquisitionCommand.connect)
        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("retry", {})))
        assert not bus.events[-1].data.ok
        assert data.get_data(0) is None

    asyncio.run(scenario())


def test_fresh_graph_has_no_stale_failure_latch():
    async def scenario():
        acquisition = AcquisitionRuntime(
            Source(), queue.Queue(), DataManager(queue.Queue()), Bus(),
            source_id="test-source", preparation_required=False,
        )
        await acquisition._on_stop_stream()
        acquisition._require_healthy_stream()
        assert acquisition._fetch_failure is None

    asyncio.run(scenario())
