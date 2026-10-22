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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_automatic_rolling_preview_retains_latest_chunks_without_changing_main_stream():
    commands, diagnostics, main = Queue(), PreviewChannel(), Queue()
    cache = PreviewCache(diagnostics, generation="g")
    routes = {EventType.GetAcquisitionPreview: AcquisitionCommand.get_preview}
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
        started = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview", params={"generation": "g"})
        assert started.status_code == 200, started.text
        initial = started.json()["data"]
        capture_id = initial["capture_id"]
        assert capture_id
        assert initial["continuous"] is True and initial["status"] == "capturing"
        assert initial["observed"] == 0
        params = {"generation": "g", "capture_id": capture_id}

        frames = [np.array([
            [100 * sequence + offset for offset in range(4)],
            [100 * sequence + 10 + offset for offset in range(4)],
            [0, sequence, 0, sequence + 1],
        ], dtype=np.int64) for sequence in range(40)]
        expected = [frame.copy() for frame in frames]
        async with asyncio.timeout(10):
            for sequence, frame in enumerate(frames):
                await source.input.put(frame)
                while cache.read(capture_id, sequence)["chunk"] is None:
                    await asyncio.sleep(0.005)

        response = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview", params=params)
        assert response.status_code == 200, response.text
        status = response.json()["data"]
        assert status["continuous"] is True and status["status"] == "capturing"
        assert status["observed"] == status["enqueued"] == 40
        assert status["received"] == 32 and status["dropped"] == 0
        assert status["stream_chunk_count"] == 40 and status["stream_sample_count"] == 160
        assert [item["sequence_id"] for item in status["chunks"]] == list(range(8, 40))
        evicted = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview/chunks/0", params=params)
        assert evicted.status_code == 404
        detail = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview/chunks/39", params=params)
        assert detail.status_code == 200, detail.text
        latest = detail.json()["data"]
        assert latest["values"] == expected[39].tolist()
        assert latest["row_indices"] == [0, 1, 2]
        assert latest["original_shape"] == [3, 4] and latest["sample_start"] == 0
        assert latest["sample_count"] == 4 and latest["trigger_row_index"] == 2
        assert latest["dtype"] == "int64"

        await runtime._on_stop_stream()
        stopped = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview", params=params)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["status"] == "finished"
        assert stopped.json()["data"]["reason"] == "source_stopped"
        retained = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview/chunks/39", params=params)
        assert retained.status_code == 200, retained.text
        assert retained.json()["data"] == latest

        assert main.qsize() == 40
        for sequence, original in enumerate(expected):
            delivered = main.get_nowait()
            assert delivered.sequence_id == sequence
            np.testing.assert_array_equal(delivered.raw_data, original)
            np.testing.assert_array_equal(frames[sequence], original)
        assert main.empty()

        await runtime._on_start_stream()
        restarted = await asyncio.to_thread(client.get, "/api/v1/acquisition/preview", params={"generation": "g"})
        assert restarted.status_code == 200, restarted.text
        next_capture = restarted.json()["data"]
        assert next_capture["capture_id"] != capture_id
        assert next_capture["continuous"] is True and next_capture["status"] == "capturing"
        assert next_capture["observed"] == next_capture["received"] == 0
    finally:
        runtime.should_exit = True
        await loop_task
        await runtime.shutdown()
        await cache.stop()
        diagnostics.close()
        client.close()
