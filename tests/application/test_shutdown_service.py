"""Backend closing uses the same complete stop as the Engine control."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnibci.application.services.shutdown_service import BackendShutdownService


def _service(calls):
    async def configuration():
        calls.append("config")

    async def engine():
        calls.append("engine")

    return BackendShutdownService(engine=SimpleNamespace(shutdown=engine),
                                  configuration=SimpleNamespace(wait_for_idle=configuration))


@pytest.mark.asyncio
async def test_waits_for_owned_configuration_then_uses_shared_engine_stop():
    calls, progress = [], []
    service = _service(calls)
    assert await service.prepare(progress=progress.append) == []
    assert calls == ["config", "engine"]
    assert progress == ["waiting_for_configuration", "stopping_engine", "stopped"]
    assert await service.prepare() == []
    assert calls.count("engine") == 1
    service.resume()
    await service.prepare()
    assert calls.count("engine") == 2


@pytest.mark.asyncio
async def test_configuration_failure_does_not_skip_engine_cleanup():
    service = _service([])
    service.configuration.wait_for_idle = AsyncMock(side_effect=RuntimeError("configuration failed"))
    service.engine.shutdown = AsyncMock(side_effect=RuntimeError("engine failed"))
    assert await service.prepare() == ["configuration: configuration failed", "engine: engine failed"]
    service.engine.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_configuration_finishes_before_engine_stop():
    service = _service([])
    finished = asyncio.Event()
    service.configuration.wait_for_idle = finished.wait
    service.engine.shutdown = AsyncMock()
    task = asyncio.create_task(service.prepare())
    await asyncio.sleep(0)
    service.engine.shutdown.assert_not_awaited()
    finished.set()
    assert await task == []
    service.engine.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_stop_can_be_retried_without_claiming_completion():
    service = _service([])
    service.engine.shutdown = AsyncMock(side_effect=[RuntimeError("output stop unconfirmed"), None])
    assert await service.prepare() == ["engine: output stop unconfirmed"]
    assert await service.prepare() == []
    assert service.engine.shutdown.await_count == 2
