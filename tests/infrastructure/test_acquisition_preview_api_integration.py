"""A real acquisition capture reaches the REST inspector without consuming main data."""
import asyncio
from queue import Queue
from types import SimpleNamespace

from fastapi.testclient import TestClient
import numpy as np
import pytest

from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.acquisition.preview_cache import PreviewCache
from omnibci.infrastructure.acquisition.preview_channel import PreviewChannel
from omnibci.presentation.dependencies import get_acquisition_service
from omnibci.presentation.main import create_app
from omnibci.shared.events import EventType


class Source:
    is_connected = True
    is_streaming = False

    def __init__(self):
        self.input = asyncio.Queue()

    async def start_stream(self):
        self.is_streaming = True

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False

    async def read_chunk(self):
        return await self.input.get()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_capture_round_trip_preserves_selected_values_and_main_stream():
    commands, diagnostics, main = Queue(), PreviewChannel(), Queue()
    cache = PreviewCache(diagnostics, generation="g")
    routes = {
        EventType.StartAcquisitionPreview: AcquisitionCommand.start_preview,
        EventType.StopAcquisitionPreview: AcquisitionCommand.stop_preview,
        EventType.GetAcquisitionPreview: AcquisitionCommand.get_preview,
    }
    broker = CommandResultBroker(SimpleNamespace(
        is_engine_ready=True,
        dispatch=lambda event: commands.put((routes[event.type], event.data)),
    ), timeout_s=2)
    service = AcquisitionAppService(broker, preview_provider=cache.read, generation_provider=lambda: "g")

    def publish(event):
        if event.type is EventType.CommandResult:
            broker.on_result(event)
        elif event.type is EventType.AcquisitionState:
            service.on_acquisition_state(event)

    source = Source()
    runtime = AcquisitionRuntime(source, commands, SimpleNamespace(put_data=main.put_nowait),
                                 SimpleNamespace(put_event=publish), source_id="test", preparation_required=False,
                                 generation="g", config_revision="a" * 64, preview_queue=diagnostics)
    app = create_app()
    app.dependency_overrides[get_acquisition_service] = lambda: service
    client = TestClient(app)
    await cache.start()
    loop_task = asyncio.create_task(runtime.main_loop())
    try:
        await runtime._on_start_stream()
        started = await asyncio.to_thread(client.post, "/api/v1/acquisition/preview/start", json={
            "generation": "g", "expected_revision": "a" * 64, "connection_id": runtime.connection_id,
            "selection": {"rows": [1, 0], "sample_start": 1, "sample_count": 2, "chunk_count": 1},
        })
        assert started.status_code == 200, started.text
        capture_id = started.json()["data"]["capture_id"]
        raw = np.array([[0, 2**60, 3], [4, 5, 6], [0, 7, 0]], dtype=np.int64)
        await source.input.put(raw)
        async with asyncio.timeout(2):
            while not cache.read(capture_id)["chunks"]:
                await asyncio.sleep(0.01)
        params = {"generation": "g", "capture_id": capture_id}
        response = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview", params=params)
        status = response.json()["data"]
        assert status["status"] == "finished" and status["reason"] == "chunk_limit"
        assert status["observed"] == status["enqueued"] == status["received"] == 1
        assert status["stream_sample_count"] == 3
        detail = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview/chunks/0", params=params)
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["row_indices"] == [1, 0, 2]
        assert data["values"] == [[5, 6], [str(2**60), 3], [7, 0]]
        assert data["original_shape"] == [3, 3] and data["sample_start"] == 1
        assert isinstance(data["timestamp_ns"], str)
        assert data["descriptor"] is None
        np.testing.assert_array_equal(main.get_nowait().raw_data, raw)
        assert source.is_streaming
        stopped = await asyncio.to_thread(client.post, "/api/v1/acquisition/preview/stop", json=params)
        assert stopped.status_code == 200 and source.is_streaming
        missing = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview/chunks/99", params=params)
        assert missing.status_code == 404
        stale = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview/chunks/0",
                                       params={**params, "generation": "old"})
        assert stale.status_code == 409
    finally:
        runtime.should_exit = True
        await loop_task
        await runtime.shutdown()
        await cache.stop()
        diagnostics.close()
        client.close()
