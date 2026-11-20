"""Only explicitly declared live sources resume without replay checkpoints."""
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.domain.ports.acquisition import SourceCheckpoint
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime, AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import Curry8DataReader, ClientRequest
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader, scan45_message_manager


@pytest.mark.parametrize("source_type,mode", [(FileReader, "checkpoint"), (Scan45DataReader, "live-current"),
                                             (Curry8DataReader, "live-current")])
def test_builtin_sources_declare_one_resume_mode(source_type, mode):
    parameters = {"data_file_path": "sample.cnt"} if source_type is FileReader else {}
    assert source_type(parameters=parameters).resume_mode == mode


@pytest.mark.parametrize("mode,checkpoint,ok", [
    ("restart-only", None, True), ("live-current", None, True), ("checkpoint", None, False),
    ("checkpoint", {}, False),
    ("checkpoint", SourceCheckpoint(resource_id="file", resource_revision="rev", state={}), True),
    ("live-current", SourceCheckpoint(resource_id="file", resource_revision="rev", state={}), False),
])
def test_checkpoint_receipt_validates_the_declared_mode(mode, checkpoint, ok):
    source = SimpleNamespace(trigger_mode="events", resume_mode=mode, checkpoint=lambda: checkpoint)
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
    source = Scan45DataReader(parameters={})
    source.writer = SimpleNamespace(write=MagicMock(), drain=AsyncMock())
    source._is_connected = True
    source._packets_counter = 12
    source._previous_event = 7
    await source.start_stream()
    await source.stop_stream()
    await source.start_stream()
    assert [call.args[0] for call in source.writer.write.call_args_list] == [
        scan45_message_manager.startSendingDataCommand,
        scan45_message_manager.stopSendingDataCommand,
        scan45_message_manager.startSendingDataCommand,
    ]
    assert source._packets_counter == 12 and source._previous_event == 7


@pytest.mark.asyncio
async def test_curry8_restart_only_requests_live_delivery():
    source = Curry8DataReader(parameters={})
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
