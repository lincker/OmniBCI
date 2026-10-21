"""Shutdown drains live work and attempts every cleanup after failures."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.application.services.shutdown_service import BackendShutdownService


def _service(calls):
    async def configuration():
        calls.append("config")

    async def engine():
        calls.append("engine")

    return BackendShutdownService(
        engine=SimpleNamespace(shutdown=engine),
        stimulation=SimpleNamespace(is_active=True, handle_stop_stimulation=Mock(side_effect=lambda _: calls.append("stimulus"))),
        stimulation_process=SimpleNamespace(stop=Mock(side_effect=lambda: calls.append("stim process"))),
        actuation_process=SimpleNamespace(stop=Mock(side_effect=lambda: calls.append("output"))),
        acquisition=SimpleNamespace(is_streaming=True, handle_stop_and_drain=Mock(side_effect=lambda: calls.append("drain"))),
        configuration=SimpleNamespace(wait_for_idle=configuration),
    )


@pytest.mark.asyncio
async def test_stops_producers_and_drains_before_engine_then_is_idempotent():
    calls = []
    service = _service(calls)
    progress = []
    assert await service.prepare(progress=progress.append) == []
    assert calls == ["config", "stimulus", "stim process", "output", "drain", "engine"]
    assert progress == ["waiting_for_configuration", "stopping_stimulation", "stopping_stimulation_process",
                        "stopping_actuation_process", "draining_acquisition", "stopping_engine", "stopped"]
    assert await service.prepare() == []
    assert calls.count("engine") == 1
    service.resume()
    await service.prepare()
    assert calls.count("engine") == 2


@pytest.mark.asyncio
async def test_failed_cleanup_does_not_skip_drain_or_engine():
    calls = []
    service = _service(calls)
    service.configuration.wait_for_idle = AsyncMock()
    service.engine.shutdown = AsyncMock(side_effect=RuntimeError("engine failed"))
    service.stimulation_process.stop.side_effect = RuntimeError("stim failed")
    service.actuation_process.stop.side_effect = RuntimeError("output failed")
    errors = await service.prepare()
    assert errors == ["stimulation process: stim failed", "actuation process: output failed", "engine: engine failed"]
    service.acquisition.handle_stop_and_drain.assert_called_once()
    service.engine.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_configuration_finishes_before_any_devices_stop():
    calls = []
    service = _service(calls)
    finished = asyncio.Event()
    service.configuration.wait_for_idle = finished.wait
    service.engine.shutdown = AsyncMock()
    task = asyncio.create_task(service.prepare())
    await asyncio.sleep(0)
    assert calls == []
    finished.set()
    assert await task == []
    assert calls == ["stimulus", "stim process", "output", "drain"]


@pytest.mark.asyncio
async def test_failed_status_queries_do_not_skip_other_resource_cleanup():
    calls = []
    service = _service(calls)

    class BrokenStimulation:
        @property
        def is_active(self):
            raise RuntimeError("stimulation state unavailable")

    class BrokenAcquisition:
        @property
        def is_streaming(self):
            raise RuntimeError("acquisition state unavailable")

    service.stimulation = BrokenStimulation()
    service.acquisition = BrokenAcquisition()
    errors = await service.prepare()
    assert errors == ["stimulation: stimulation state unavailable", "acquisition drain: acquisition state unavailable"]
    assert calls == ["config", "stim process", "output", "engine"]
