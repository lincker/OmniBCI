"""A new file read cannot reuse queued samples or epochs from its previous read."""

import asyncio
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest

from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand, DisconnectDataProviderCommand, StartStreamCommand,
)
from omnibci.application.events import CommandRequest
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader
from omnibci.shared.events import EventType
from tests.infrastructure.test_stream_drain import _Decoder, _processor


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
async def test_restart_after_eof_drains_old_samples_before_preparing(reconnect, monkeypatch):
    raw = np.array([[10, 11, 12, 13], [1, 0, 0, 0]])
    source = FileReader(parameters={"data_file_path": "test.cnt", "file_reader_chunk_size": 2})

    async def read_chunk(*, start, end):
        return raw[:, start:end] if start < raw.shape[1] else None

    async def connect():
        source._is_connected = True

    monkeypatch.setattr(source, "connect", connect)
    source.reader_adapter = SimpleNamespace(read_chunk=read_chunk, close=AsyncMock())
    source._is_connected = True
    data = DataManager(Queue())
    events = []

    def publish(event):
        events.append(event)
        if event.type is EventType.AcquisitionState:
            service.on_acquisition_state(event)

    bus = SimpleNamespace(put_event=publish)
    acquisition = AcquisitionRuntime(source, Queue(), data, bus, source_id="file_reader",
                                     preparation_required=False, generation="generation-1",
                                     config_revision="revision-1")
    decoder = _Decoder(samples=10)
    processor = _processor(decoder=decoder, bus=bus, data=data)
    loop = asyncio.get_running_loop()
    commands = []

    async def dispatch(event_type, payload):
        command = {
            EventType.StopAndDrain: AcquisitionCommand.stop_and_drain,
            EventType.StartStream: AcquisitionCommand.start_stream,
            EventType.ConnectDataProvider: AcquisitionCommand.connect,
            EventType.DisconnectDataProvider: AcquisitionCommand.disconnect,
        }[event_type]
        command_id = str(len(commands))
        commands.append(event_type)
        await acquisition.execute((command, CommandRequest(command_id, payload)))
        for _ in range(10):
            result = next((event.data for event in events
                           if event.type is EventType.CommandResult and event.data.command_id == command_id), None)
            if result is not None:
                assert result.ok, result.message
                return result
            await processor._current_state.run()
        raise AssertionError("The processor did not acknowledge the stream boundary")

    def request(event_type, payload=None, **kwargs):
        return asyncio.run_coroutine_threadsafe(dispatch(event_type, payload), loop).result(timeout=3)

    service = AcquisitionAppService(SimpleNamespace(request=request))
    prepared = []

    def prepare():
        prepared.append((processor.current_epoch, processor.pretrigger._buf))

    service.set_signal_preparer(prepare)
    try:
        await asyncio.to_thread(service.handle_start_stream, StartStreamCommand())
        await acquisition.fetch_task
        assert not service.is_streaming
        await processor._current_state.run()
        assert processor.current_epoch is not None
        assert not data.empty()
        if reconnect:
            await asyncio.to_thread(service.handle_disconnect, DisconnectDataProviderCommand())
            await asyncio.to_thread(service.handle_connect, ConnectDataProviderCommand())

        await asyncio.to_thread(service.handle_start_stream, StartStreamCommand())
        assert len(prepared) == 2
        assert all(epoch is None and history is None for epoch, history in prepared)
        await acquisition.fetch_task
        while not data.empty():
            await processor._current_state.run()
        assert decoder.calls == 0
        assert processor.completed_trials == 0
        assert processor.current_epoch is not None
    finally:
        await acquisition.shutdown()
