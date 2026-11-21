"""Worker and external-node entry points reject malformed control payloads."""
import asyncio
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.application.events import CommandRequest
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.stimulation.application import Stimulation
from omnibci.shared.events import Event, EventType
from tests.actuation.test_application import _app
from tests.infrastructure.test_source_contract import Source, runtime


@pytest.mark.asyncio
async def test_source_rejects_invalid_payload_before_connecting():
    source = Source()
    owner = runtime(source)
    events = []
    owner.event_bus = SimpleNamespace(put_event=events.append)
    await owner.execute((AcquisitionCommand.connect, CommandRequest("connect", {"ignored": True})))
    assert not source.calls
    assert not events[-1].data.ok
    assert events[-1].data.error_code == "INVALID_COMMAND_PAYLOAD"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,method", [
    (EventType.SettleActuation, "_request_settlement"),
    (EventType.ResumeActuation, "_request_resume"),
])
async def test_actuation_rejects_invalid_payload_before_reserving_device(event_type, method):
    app = _app()
    app._settle_actuation = AsyncMock()
    app._resume_actuation = AsyncMock()
    getattr(app, method)(Event(event_type, CommandRequest("control", {"ignored": True})))
    await asyncio.sleep(0)
    assert not app._paused
    app._settle_actuation.assert_not_called()
    app._resume_actuation.assert_not_called()
    assert app.event_bus.events[-1].data.error_code == "INVALID_COMMAND_PAYLOAD"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,method", [
    (EventType.PauseStimulation, "_pause_stimulation"),
    (EventType.StopStimulation, "_stop_stimulation_system"),
])
async def test_stimulation_rejects_invalid_payload_before_stopping(event_type, method):
    app = Stimulation.__new__(Stimulation)
    events = []
    app.event_bus = SimpleNamespace(put_event=events.append)
    app._paused = app.should_stop = False
    app._stop_requests = []
    app.stimulator = Mock()
    app.stim_command_queue = Queue()
    app.result_command_queue = Queue()
    app._render_task = asyncio.create_task(asyncio.sleep(0))
    try:
        await getattr(app, method)(Event(event_type, CommandRequest("control", {"ignored": True})))
        assert not app._paused and not app.should_stop
        app.stimulator.request_stop.assert_not_called()
        assert events[-1].data.error_code == "INVALID_COMMAND_PAYLOAD"
    finally:
        await app._render_task
