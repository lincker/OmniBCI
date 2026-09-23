"""Only explicitly declared live sources resume without replay checkpoints."""
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.acquisition.acquisition import Acquisition, AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.curry8 import Curry8DataReader, ClientRequest
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45DataReader, scan45_message_manager


@pytest.mark.parametrize("source_type,safe", [(FileReader, False), (Scan45DataReader, True), (Curry8DataReader, True)])
def test_builtin_sources_declare_live_resume_separately_from_checkpoint(source_type, safe):
    source = source_type(parameters={})
    assert source.resume_without_checkpoint_safe is safe
    assert source.supports_checkpoint is (source_type is FileReader)


@pytest.mark.parametrize("declared,expected", [(None, False), (False, False), ("true", False), (True, True)])
def test_checkpoint_receipt_exposes_only_explicit_live_resume_guarantee(declared, expected):
    source = SimpleNamespace(supports_checkpoint=False, is_streaming=False)
    if declared is not None:
        source.resume_without_checkpoint_safe = declared
    events = []
    acquisition = Acquisition(source, Queue(), DataManager(Queue()),
                              SimpleNamespace(put_event=events.append), generation="run", config_revision="revision")
    acquisition._configuration_command(AcquisitionCommand.get_checkpoint, CommandRequest("inspect", {
        "generation": "run", "expected_revision": "revision",
    }))
    receipt = events[-1].data
    assert receipt.ok
    assert receipt.data == {"generation": "run", "config_revision": "revision", "supported": False,
                            "checkpoint": None, "resume_without_checkpoint_safe": expected}


@pytest.mark.asyncio
async def test_scan45_restart_requests_live_delivery_without_resetting_sample_state():
    source = Scan45DataReader(parameters={})
    source.writer = SimpleNamespace(write=MagicMock(), drain=AsyncMock())
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
