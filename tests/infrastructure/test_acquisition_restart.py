"""A new file read cannot reuse queued samples or epochs from its previous read."""

import asyncio
from queue import Queue
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from omnibci.application.events import ProcessingInputStart
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader
from omnibci.shared.events import EventType
from tests.infrastructure.test_stream_drain import _Decoder, _processor


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
async def test_eof_boundary_separates_reads_without_waiting_for_processing(reconnect, monkeypatch):
    raw = np.array([[10, 11, 12, 13], [1, 0, 0, 0]])
    source = FileReader(parameters={"data_file_path": "test.cnt", "file_reader_chunk_size": 2})

    async def read_chunk(*, start, end):
        return raw[:, start:end] if start < raw.shape[1] else None

    async def connect():
        source.reader_adapter = SimpleNamespace(read_chunk=read_chunk, close=AsyncMock())

    monkeypatch.setattr(source, "connect", connect)
    gate = ThreadEvent()
    gate.set()
    data = DataManager(Queue(), gate)
    events = []
    bus = SimpleNamespace(put_event=events.append)
    acquisition = AcquisitionRuntime(source, Queue(), data, bus, source_id="file_reader",
                                     generation="generation-1",
                                     acquisition_revision="revision-1", engine_revision="engine",
                                     processing_active=gate)
    decoder = _Decoder(samples=10)
    processor = _processor(decoder=decoder, bus=bus, data=data)
    processor._signal_receipt = acquisition._processing_receipt = {"input_id": "input-one"}
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await acquisition.fetch_task
        await processor._current_state.run()
        assert processor.current_epoch is not None
        assert not data.empty()
        if reconnect:
            await acquisition._on_disconnect()
            await acquisition._on_connect()
            data.put_data(ProcessingInputStart("reattach", processor._signal_receipt, acquisition._last_sequence))
            gate.set()
        # The source restarts while P has not consumed its previous tail.
        await acquisition._on_start_stream()
        await acquisition.fetch_task
        while not data.empty():
            await processor._current_state.run()
        assert decoder.calls == 0
        assert processor.completed_trials == 0
        assert processor.current_epoch is None
        records = [event.data for event in events if event.type is EventType.EpochProcessed]
        assert len(records) == 2
        assert all(record.status == "interrupted" and record.reason == "stream_stop" for record in records)
    finally:
        await acquisition.shutdown()
