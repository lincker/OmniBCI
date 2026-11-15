"""Processing lifecycle changes leave source reading and unrelated peers alone."""
import asyncio
from queue import Queue
from threading import Event as ThreadEvent
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np

import pytest

from omnibci.application.events import CommandRequest, ProcessingInputStart, StreamBarrier, StreamDrainReceipt
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
from tests.neuroengine.test_module_peer_reconfiguration import config
from tests.neuroengine.test_peer_gateway import FakeBus, FakeTransport
from tests.neuroengine.test_channel_mapping import _plan, _processor, _receipt
from tests.neuroengine.test_neuro_engine import _make_engine
from omnibci.infrastructure.signal_processing import signal_configuration
from omnibci.shared.events import Event, EventType


def test_input_boundary_is_delivered_when_sample_gate_is_closed():
    data = DataManager(Queue(), processing_active=ThreadEvent())
    boundary = StreamBarrier("detach", "engine", "acquisition", 3)
    data.put_data(boundary)
    assert data.get_data(timeout=0.001) == boundary


@pytest.mark.asyncio
async def test_source_can_start_without_processing_preparation():
    gate = ThreadEvent()
    gate.set()
    source = SimpleNamespace(is_connected=True, is_streaming=False,
                             start_stream=AsyncMock(), read_chunk=AsyncMock())
    source.start_stream.side_effect = lambda: setattr(source, "is_streaming", True)
    acquisition = AcquisitionRuntime(
        source, Queue(), DataManager(Queue(), gate), source_id="test",
        processing_active=gate,
    )
    acquisition.fetch = AsyncMock()
    await acquisition._on_start_stream()
    await acquisition.fetch_task
    assert source.is_streaming


@pytest.mark.asyncio
async def test_slow_stimulation_replacement_does_not_block_actuation():
    gateway = PeerGateway(FakeBus())
    gateway.add_peer(config("stimulation", FakeTransport()))
    gateway.add_peer(config("actuation", FakeTransport()))
    await gateway.start()
    entered, release = asyncio.Event(), asyncio.Event()
    replacing = asyncio.create_task(gateway.replace_peer(
        "stimulation", config("stimulation", FakeTransport(start_entered=entered, start_release=release))))
    try:
        await entered.wait()
        await asyncio.wait_for(gateway.replace_peer("actuation", config("actuation", FakeTransport())), 0.1)
    finally:
        release.set()
        await replacing
        await gateway.stop()


def input_pair(monkeypatch, data=None):
    plan = _plan()
    if data is None:
        data = DataManager(Queue(), processing_active=ThreadEvent())
    gate = data.processing_active
    events = []
    source = SimpleNamespace(is_connected=True, is_streaming=True,
                             start_stream=AsyncMock(), stop_stream=AsyncMock())
    acquisition = AcquisitionRuntime(
        source, Queue(), data, SimpleNamespace(put_event=events.append),
        source_id=plan.source.source_id,
        processing_active=gate, generation="generation-one", system_revision="system",
        acquisition_revision=plan.acquisition_revision,
    )
    acquisition.connection_id = plan.source.connection_id
    acquisition._descriptor = plan.source
    processor = _processor(plan)
    processor.data_manager = data
    processor.event_bus = SimpleNamespace(put_event=events.append)
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", MagicMock(return_value=(object(), object(), [])))
    receipt = {**_receipt(plan), "input_id": "input-one"}
    processor._prepare_signal(CommandRequest("prepare", {"plan": plan.to_dict(), "receipt": receipt}))
    assert events.pop().data.ok
    return acquisition, processor, data, receipt, events


@pytest.mark.asyncio
async def test_attach_and_detach_use_fifo_without_stopping_source_or_replaying_gap(monkeypatch):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    acquisition._last_sequence = 4
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("attach", receipt)))
    assert not events  # Only the consumer can confirm successful attachment.
    await processor.next_chunk()
    assert events.pop().data.data == {**receipt, "attached": True, "last_sequence": 4}
    assert processor._last_sequence == 4
    data.put_data(SignalChunk(0, 5, np.zeros((4, 3))))
    acquisition._last_sequence = acquisition._last_processing_sequence = 5
    identity = {"generation": acquisition.generation, "system_revision": acquisition.system_revision,
                "expected_revision": acquisition.acquisition_revision}
    await acquisition.execute((AcquisitionCommand.detach_processing, CommandRequest("detach", identity)))
    assert not data.has_consumer
    # A keeps advancing after the boundary. This sample is never queued for P.
    data.put_data(SignalChunk(0, 6, np.zeros((4, 3))))
    acquisition._last_sequence = 6
    assert (await processor.next_chunk()).sequence_id == 5
    await processor.next_chunk()
    result = events.pop().data
    assert result.ok and result.command == EventType.DetachProcessingInput.name
    assert StreamDrainReceipt.from_value(result.data).last_sequence == 5
    assert not processor._input_attached
    assert data.empty()
    receipt = {**receipt, "input_id": "input-two"}
    processor._signal_receipt = receipt
    await acquisition.execute((AcquisitionCommand.attach_processing, CommandRequest("reattach", receipt)))
    await processor.next_chunk()
    assert processor._last_sequence == 6
    assert events.pop().data.ok
    acquisition.data_source.stop_stream.assert_not_called()
    acquisition.data_source.start_stream.assert_not_called()
    assert acquisition.data_source.is_streaming


@pytest.mark.asyncio
async def test_unprepared_or_stale_input_does_not_reach_processing(monkeypatch):
    acquisition, processor, data, receipt, events = input_pair(monkeypatch)
    data.buffer.put(ProcessingInputStart("stale", {**receipt, "input_id": "old-input"}, 10))
    await processor.next_chunk()
    assert not events.pop().data.ok
    data.buffer.put(SignalChunk(0, 11, np.zeros((4, 3))))
    assert await processor.next_chunk() is None
    assert not processor._input_attached
    await acquisition.execute((AcquisitionCommand.attach_processing,
                               CommandRequest("stale-source", {**receipt, "connection_id": "other-source"})))
    assert not events.pop().data.ok
    assert not data.has_consumer and data.empty()


@pytest.mark.asyncio
async def test_failed_role_cleanup_does_not_block_another_peer():
    gateway = PeerGateway(FakeBus())
    await gateway.start()
    broken = FakeTransport(fail_start=True, fail_stop=True)
    try:
        with pytest.raises(BaseExceptionGroup):
            await gateway.replace_peer("stimulation", config("stimulation", broken))
        await gateway.replace_peer("actuation", config("actuation", FakeTransport()))
        assert gateway.is_peer_ready("actuation")
        assert gateway.peer("stimulation").has_lifecycle_resources
    finally:
        broken.fail_stop = False
        await gateway.stop()


@pytest.mark.asyncio
async def test_processing_readiness_wait_does_not_block_acquisition_stop():
    engine = _make_engine()
    for worker in (engine.acquisition_worker, engine.signal_processing_worker):
        worker.is_alive.return_value = True
        worker.join.side_effect = lambda worker=worker, **_: setattr(worker.is_alive, "return_value", False)
    await engine._start()
    await engine.start_module("acquisition", {})
    entered, release = asyncio.Event(), asyncio.Event()
    async def wait_ready(channels):
        entered.set()
        await release.wait()
    engine._wait_for_workers = wait_ready
    starting = asyncio.create_task(engine.start_module("processing", {}))
    try:
        await entered.wait()
        await asyncio.wait_for(engine.stop_module("acquisition"), 0.1)
        assert not engine.is_module_running("acquisition")
        assert not engine.is_module_running("processing")
    finally:
        release.set()
        await starting
        await engine._stop()


def test_source_reset_rejects_old_identity_and_preserves_processing_runtime(monkeypatch):
    _, processor, _, receipt, events = input_pair(monkeypatch)
    processor._reset_input(CommandRequest("wrong-source", {
        "generation": "generation-one", "system_revision": "system",
        "expected_acquisition_revision": "old-source",
    }))
    assert not events.pop().data.ok
    assert processor._signal_receipt == receipt
    processor._reset_input(CommandRequest("old-input-fault", {
        "generation": "generation-one", "system_revision": "system",
        "expected_acquisition_revision": receipt["acquisition_revision"], "input_id": "old-input",
    }))
    assert not events.pop().data.ok
    assert processor._signal_receipt == receipt
    processor._reset_input(CommandRequest("source-exited", {
        "generation": "generation-one", "system_revision": "system",
        "expected_acquisition_revision": receipt["acquisition_revision"],
    }))
    result = events.pop().data
    assert result.ok and result.data["attached"] is False
    assert "drained" not in result.data
    assert processor._signal_plan is None and processor._signal_receipt is None
    assert not processor._should_stop


@pytest.mark.asyncio
async def test_full_processing_queue_does_not_stop_acquisition_or_preview():
    gate = ThreadEvent()
    gate.set()
    data = DataManager(Queue(maxsize=1), gate)
    events = []
    source = SimpleNamespace(is_connected=True, is_streaming=True,
                             read_chunk=AsyncMock(side_effect=[np.zeros((2, 3)) for _ in range(3)] + [None]),
                             stop_stream=AsyncMock())
    source.stop_stream.side_effect = lambda: setattr(source, "is_streaming", False)
    acquisition = AcquisitionRuntime(source, Queue(), data, SimpleNamespace(put_event=events.append),
                                     source_id="source", generation="run", system_revision="system",
                                     acquisition_revision="acquisition", processing_active=gate)
    acquisition._processing_receipt = {"input_id": "input-one"}
    acquisition.preview = SimpleNamespace(active=True, offer=MagicMock(), finish=MagicMock())
    await acquisition.fetch()
    assert source.read_chunk.await_count == 4
    assert acquisition.stream_chunk_count == 3
    assert acquisition.preview.offer.call_count == 3
    assert acquisition._fetch_failure is None
    assert not gate.is_set()
    faults = [event for event in events if event.type.name == "ProcessingInputFault"]
    assert len(faults) == 1 and faults[0].data["input_id"] == "input-one"


def test_engine_routes_input_fault_with_attachment_identity_only_to_processing():
    engine = _make_engine(generation="run", system_revision="system",
                          module_revisions={"acquisition": "acquisition", "processing": "processing"})
    engine._ready_modules.add("processing")
    engine.signal_processing_worker.is_alive.return_value = True
    fault = {"generation": "run", "system_revision": "system", "acquisition_revision": "acquisition",
             "input_id": "failed-input", "reason": "queue full"}
    engine._on_processing_input_fault(Event(EventType.ProcessingInputFault, fault))
    command, request = engine.processor_command_queue.put_nowait.call_args.args[0]
    assert command.value == "reset_processing_input"
    assert request.payload["input_id"] == "failed-input"
    assert request.payload["expected_acquisition_revision"] == "acquisition"
    engine.acquisition_command_queue.put_nowait.assert_not_called()
    engine.processor_command_queue.reset_mock()
    engine._on_processing_input_fault(Event(EventType.ProcessingInputFault, {**fault, "generation": "old-run"}))
    engine.processor_command_queue.put_nowait.assert_not_called()
