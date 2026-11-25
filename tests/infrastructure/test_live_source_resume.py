"""Live packet framing survives a cancelled read without losing its header."""
import asyncio
import struct
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import (
    Curry8DataReader, Curry8Header, Curry8Metadata,
)
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import (
    Scan45DataReader, Scan45Message,
)


@pytest.fixture(params=["scan45", "curry8"])
def protocol(request):
    if request.param == "scan45":
        return Scan45DataReader, "_load_package", lambda size: Scan45Message.Header("DATA", 2, 2, size)
    return Curry8DataReader, "_read_packet", lambda size: Curry8Header(b"DATA", 2, 1, body_size=size)


async def cancel_read(read):
    pending = asyncio.create_task(read())
    await asyncio.sleep(0)
    assert not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


def stream_writer():
    writer = Mock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return writer


@pytest.mark.parametrize("partial_body_size", [0, 7])
@pytest.mark.parametrize("restart_stream", [False, True])
def test_resume_completes_interrupted_body_before_reading_next_header(protocol, partial_body_size, restart_stream):
    async def scenario():
        source_type, method, make_header = protocol
        source = source_type(parameters={}, resource_root=Path(__file__).parent)
        source.reader = asyncio.StreamReader()
        read = getattr(source, method)
        first_body, next_body = bytes(40), bytes([17]) * 20
        first_header, next_header = make_header(len(first_body)), make_header(len(next_body))
        source.reader.feed_data(first_header.to_bytes() + first_body[:partial_body_size])
        await cancel_read(read)
        if restart_stream:
            source.writer = stream_writer()
            source._is_connected = source._is_streaming = True
            if isinstance(source, Curry8DataReader):
                source._metadata = Curry8Metadata(803, 2, 1000, 4, False, False, ("Cz", "Trigger"))
            await source.stop_stream()
            await source.start_stream()
        source.reader.feed_data(first_body[partial_body_size:] + next_header.to_bytes() + next_body)

        actual_header, actual_body = await asyncio.wait_for(read(), timeout=1)
        assert actual_header.to_bytes() == first_header.to_bytes()
        assert actual_body == first_body
        actual_header, actual_body = await asyncio.wait_for(read(), timeout=1)
        assert actual_header.to_bytes() == next_header.to_bytes()
        assert actual_body == next_body

    asyncio.run(scenario())


def test_new_connection_clears_pending_packet(protocol, monkeypatch):
    async def scenario():
        source_type, method, make_header = protocol
        source = source_type(parameters={}, resource_root=Path(__file__).parent)
        source.reader = asyncio.StreamReader()
        read = getattr(source, method)
        source.reader.feed_data(make_header(40).to_bytes())
        await cancel_read(read)

        reader, writer = asyncio.StreamReader(), stream_writer()
        if isinstance(source, Curry8DataReader):
            reader.feed_data(Curry8Header(b"DATA", 1, 1, body_size=4).to_bytes() + struct.pack("<i", 803))
        body = b"new connection"
        header = make_header(len(body))
        reader.feed_data(header.to_bytes() + body)
        monkeypatch.setattr(asyncio, "open_connection", AsyncMock(return_value=(reader, writer)))
        await source.connect()
        assert source._pending_header is None
        actual_header, actual_body = await asyncio.wait_for(read(), timeout=1)
        assert actual_header.to_bytes() == header.to_bytes()
        assert actual_body == body

    asyncio.run(scenario())


@pytest.mark.parametrize("source_type_name,mode", [
    ("FileReader", "checkpoint"),
    ("Scan45DataReader", "live-current"),
    ("Curry8DataReader", "live-current"),
])
def test_builtin_sources_declare_one_resume_mode(source_type_name, mode):
    from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader

    source_type = {"FileReader": FileReader, "Scan45DataReader": Scan45DataReader, "Curry8DataReader": Curry8DataReader}[source_type_name]
    parameters = {"data_file_path": "sample.cnt"} if source_type is FileReader else {}
    assert source_type(parameters=parameters, resource_root=Path(__file__).parent).resume_mode == mode


@pytest.mark.parametrize("mode,checkpoint,ok", [
    ("restart-only", None, True), ("live-current", None, True), ("checkpoint", None, False),
    ("checkpoint", {}, False),
    ("checkpoint", "valid", True),
    ("live-current", "valid", False),
])
def test_checkpoint_receipt_validates_the_declared_mode(mode, checkpoint, ok):
    from queue import Queue
    from types import SimpleNamespace
    from omnibci.application.events import CommandRequest
    from omnibci.domain.ports.acquisition import SourceCheckpoint
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
    from omnibci.infrastructure.ipc.data_manager import DataManager

    cp_val = SourceCheckpoint(resource_id="file", resource_revision="rev", state={}) if checkpoint == "valid" else checkpoint
    source = SimpleNamespace(trigger_mode="events", resume_mode=mode, checkpoint=lambda: cp_val)
    events = []
    acquisition = AcquisitionRuntime(
        source, Queue(), DataManager(Queue()), SimpleNamespace(put_event=events.append),
        source_id="test-source", generation="run", acquisition_revision="revision", engine_revision="engine",
    )
    acquisition._connected = True
    acquisition.connection_id = "connection"
    acquisition._configuration_command(AcquisitionCommand.get_checkpoint, CommandRequest("inspect", {
        "generation": "run", "expected_revision": "revision", "engine_revision": "engine",
    }))
    receipt = events[-1].data
    assert receipt.ok is ok
    if ok:
        assert receipt.data["resume_mode"] == mode
        assert set(receipt.data) == {"generation", "acquisition_revision", "engine_revision", "resume_mode", "checkpoint"}


@pytest.mark.asyncio
async def test_scan45_restart_requests_live_delivery_without_resetting_sample_state():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import scan45_message_manager

    source = Scan45DataReader(parameters={}, resource_root=Path(__file__).parent)
    source.writer = SimpleNamespace(write=MagicMock(), drain=AsyncMock())
    source._is_connected = True
    await source.start_stream()
    await source.stop_stream()
    await source.start_stream()
    assert [call.args[0] for call in source.writer.write.call_args_list] == [
        scan45_message_manager.startSendingDataCommand,
        scan45_message_manager.stopSendingDataCommand,
        scan45_message_manager.startSendingDataCommand,
    ]


@pytest.mark.asyncio
async def test_curry8_restart_only_requests_live_delivery():
    from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import ClientRequest

    source = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    source._is_connected = True
    source._metadata = object()
    source.writer = object()
    source._send_request = AsyncMock()
    await source.start_stream()
    await source.stop_stream()
    await source.start_stream()
    assert [call.args[0] for call in source._send_request.call_args_list] == [
        ClientRequest.STREAMING_START, ClientRequest.STREAMING_STOP, ClientRequest.STREAMING_START,
    ]


def test_disconnect_closes_writer_and_discards_pending_header(protocol):
    async def scenario():
        source_type, method, make_header = protocol
        source = source_type(parameters={}, resource_root=Path(__file__).parent)
        source.reader = asyncio.StreamReader()
        source.writer = writer = stream_writer()
        source._is_connected = True
        source.reader.feed_data(make_header(40).to_bytes())
        await cancel_read(getattr(source, method))

        await source.disconnect()
        writer.close.assert_called_once_with()
        writer.wait_closed.assert_awaited_once_with()
        assert not source._is_connected
        assert source._pending_header is None

    asyncio.run(scenario())


def test_replaced_connection_does_not_reuse_interrupted_header(protocol):
    async def scenario():
        source_type, method, make_header = protocol
        source = source_type(parameters={}, resource_root=Path(__file__).parent)
        source.reader = asyncio.StreamReader()
        read = getattr(source, method)
        source.reader.feed_data(make_header(40).to_bytes())
        await cancel_read(read)

        source.reader = asyncio.StreamReader()
        body = b"new connection"
        header = make_header(len(body))
        source.reader.feed_data(header.to_bytes() + body)
        actual_header, actual_body = await asyncio.wait_for(read(), timeout=1)
        assert actual_header.to_bytes() == header.to_bytes()
        assert actual_body == body

    asyncio.run(scenario())


@pytest.mark.parametrize("source_type_name,mode", [
    ("FileReader", "checkpoint"),
    ("Scan45DataReader", "live-current"),
    ("Curry8DataReader", "live-current"),
])
def test_builtin_sources_declare_one_resume_mode(source_type_name, mode):
    from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader

    source_type = {"FileReader": FileReader, "Scan45DataReader": Scan45DataReader, "Curry8DataReader": Curry8DataReader}[source_type_name]
    parameters = {"data_file_path": "sample.cnt"} if source_type is FileReader else {}
    assert source_type(parameters=parameters, resource_root=Path(__file__).parent).resume_mode == mode


@pytest.mark.parametrize("mode,checkpoint,ok", [
    ("restart-only", None, True), ("live-current", None, True), ("checkpoint", None, False),
    ("checkpoint", {}, False),
    ("checkpoint", "valid", True),
    ("live-current", "valid", False),
])
def test_checkpoint_receipt_validates_the_declared_mode(mode, checkpoint, ok):
    from queue import Queue
    from types import SimpleNamespace
    from omnibci.application.events import CommandRequest
    from omnibci.domain.ports.acquisition import SourceCheckpoint
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
    from omnibci.infrastructure.ipc.data_manager import DataManager

    cp_val = SourceCheckpoint(resource_id="file", resource_revision="rev", state={}) if checkpoint == "valid" else checkpoint
    source = SimpleNamespace(trigger_mode="events", resume_mode=mode, checkpoint=lambda: cp_val)
    events = []
    acquisition = AcquisitionRuntime(
        source, Queue(), DataManager(Queue()), SimpleNamespace(put_event=events.append),
        source_id="test-source", generation="run", acquisition_revision="revision", engine_revision="engine",
    )
    acquisition._connected = True
    acquisition.connection_id = "connection"
    acquisition._configuration_command(AcquisitionCommand.get_checkpoint, CommandRequest("inspect", {
        "generation": "run", "expected_revision": "revision", "engine_revision": "engine",
    }))
    receipt = events[-1].data
    assert receipt.ok is ok
    if ok:
        assert receipt.data["resume_mode"] == mode
        assert set(receipt.data) == {"generation", "acquisition_revision", "engine_revision", "resume_mode", "checkpoint"}


@pytest.mark.asyncio
async def test_scan45_restart_requests_live_delivery_without_resetting_sample_state():
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import scan45_message_manager

    source = Scan45DataReader(parameters={}, resource_root=Path(__file__).parent)
    source.writer = SimpleNamespace(write=MagicMock(), drain=AsyncMock())
    source._is_connected = True
    await source.start_stream()
    await source.stop_stream()
    await source.start_stream()
    assert [call.args[0] for call in source.writer.write.call_args_list] == [
        scan45_message_manager.startSendingDataCommand,
        scan45_message_manager.stopSendingDataCommand,
        scan45_message_manager.startSendingDataCommand,
    ]


@pytest.mark.asyncio
async def test_curry8_restart_only_requests_live_delivery():
    from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import ClientRequest

    source = Curry8DataReader(parameters={}, resource_root=Path(__file__).parent)
    source._is_connected = True
    source._metadata = object()
    source.writer = object()
    source._send_request = AsyncMock()
    await source.start_stream()
    await source.stop_stream()
    await source.start_stream()
    assert [call.args[0] for call in source._send_request.call_args_list] == [
        ClientRequest.STREAMING_START, ClientRequest.STREAMING_STOP, ClientRequest.STREAMING_START,
    ]
