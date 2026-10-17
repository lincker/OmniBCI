"""Acquisition command ordering and raw recording durability across a session."""

import asyncio
import json
from pathlib import Path
import queue

import numpy as np
import pytest

from omnibci.application.events import CommandRequest, StreamBarrier
from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.acquisition import recording as recording_module
from omnibci.infrastructure.acquisition.recording import LocalSignalRecorder, iter_recorded_chunks
from omnibci.shared.events import EventType


class Source:
    def __init__(self):
        self.is_connected = True
        self.is_streaming = False
        self.chunks = asyncio.Queue()
        self.waiting = asyncio.Event()

    async def get_metadata(self):
        return "test source"

    def describe_output(self, *, source_id, connection_id):
        return SignalInfo(source_id, 250, (ChannelDescriptor(0, "Cz"),), 1,
                          connection_id=connection_id)

    async def start_stream(self):
        self.is_streaming = True
        self.waiting.clear()

    async def read_chunk(self):
        if self.chunks.empty():
            self.waiting.set()
        return await self.chunks.get()

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False


class Bus:
    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


class Sink:
    def __init__(self):
        self.items = []

    def put_data(self, value):
        self.items.append(value)


def runtime(tmp_path):
    return AcquisitionRuntime(
        Source(), queue.Queue(), Sink(), Bus(), source_id="test-source",
        preparation_required=False, generation="generation-1", config_revision="revision-1",
        recorder=LocalSignalRecorder(tmp_path),
    )


async def command(acquisition, operation, command_id, session_id="session-1", **payload):
    await acquisition.execute((operation, CommandRequest(command_id, {"session_id": session_id, **payload})))
    return next((event.data for event in reversed(acquisition.event_bus.events)
                 if event.type is EventType.CommandResult and event.data.command_id == command_id), None)


async def begin(acquisition):
    await acquisition._on_get_metadata()
    result = await command(acquisition, AcquisitionCommand.begin_recording, "begin",
                           subject_id="subject-1", paradigm="test-paradigm")
    assert result.ok, result.message
    state = next(event.data for event in reversed(acquisition.event_bus.events)
                 if event.type is EventType.RawRecordingState)
    assert state == {**result.data, "status": "recording"}
    return result.data


def manifest(info):
    return json.loads((Path(info["path"]) / "metadata.json").read_text())


def raw():
    return np.array([[1.25, 2.5, 3.75], [0, 9, 0]], dtype=np.float32)


async def stream_one(acquisition):
    acquisition.data_source.chunks.put_nowait(raw())
    await acquisition._on_start_stream()
    await asyncio.wait_for(acquisition.data_source.waiting.wait(), timeout=1)


@pytest.mark.asyncio
async def test_full_raw_chunk_is_written_before_downstream_can_modify_it(tmp_path, monkeypatch):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    expected = raw()
    array = expected.copy()
    acquisition.data_source.chunks.put_nowait(array)
    acquisition.data_source.chunks.put_nowait(None)
    timestamp = 1_792_000_000_123_456_789
    monkeypatch.setattr("omnibci.infrastructure.acquisition.acquisition.time.time_ns", lambda: timestamp)
    original_sink = acquisition.data_manager.put_data

    def mutate_downstream(chunk):
        original_sink(chunk)
        chunk.raw_data[:] = -100

    monkeypatch.setattr(acquisition.data_manager, "put_data", mutate_downstream)
    await acquisition._on_start_stream()
    await acquisition.fetch_task
    # EOF flushes the raw bytes but keeps this session open until explicit end.
    stored = list(iter_recorded_chunks(info["path"]))
    assert len(stored) == 1
    np.testing.assert_array_equal(stored[0].raw_data, expected)
    np.testing.assert_array_equal(stored[0].triggers, expected[-1])
    assert stored[0].raw_data.dtype == expected.dtype
    assert stored[0].timestamp == timestamp
    assert manifest(info)["status"] == "recording"
    assert acquisition._recording == info
    await acquisition._on_stop_stream()
    result = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert result.ok
    assert manifest(info)["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [AcquisitionCommand.stop_stream, AcquisitionCommand.stop_and_drain])
async def test_stop_joins_producer_then_flushes_before_ack_or_barrier(tmp_path, monkeypatch, operation):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    acquisition.data_source.is_streaming = True
    trace = []
    entered = asyncio.Event()

    async def producer():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            value = SignalChunk(123, 0, raw())
            acquisition._recorder.append(value, config_revision="revision-1")
            acquisition.data_manager.put_data(value)
            acquisition._last_sequence = 0
            trace.append("producer joined")

    acquisition.fetch_task = asyncio.create_task(producer())
    producer_task = acquisition.fetch_task
    await entered.wait()
    original_fsync = recording_module.os.fsync

    def fsync(fd):
        assert producer_task.done()
        assert acquisition.fetch_task is None
        trace.append("fsync")
        return original_fsync(fd)

    monkeypatch.setattr(recording_module.os, "fsync", fsync)
    original_sink = acquisition.data_manager.put_data
    # Data and index precede the metadata checkpoint and its directory entry.
    flushed = ["producer joined"] + ["fsync"] * (3 if recording_module.os.name == "nt" else 4)

    def sink(value):
        if isinstance(value, StreamBarrier):
            assert trace == flushed
            trace.append("barrier")
        original_sink(value)

    monkeypatch.setattr(acquisition.data_manager, "put_data", sink)
    result = await command(acquisition, operation, "stop")
    if operation is AcquisitionCommand.stop_and_drain:
        assert result is None
        assert trace == flushed + ["barrier"]
        assert acquisition.data_manager.items[-1].session_id == "session-1"
    else:
        assert result.ok
        assert trace == flushed
    assert manifest(info)["status"] == "recording"
    assert len(list(iter_recorded_chunks(info["path"]))) == 1
    await command(acquisition, AcquisitionCommand.end_recording, "end")


@pytest.mark.asyncio
async def test_end_requires_matching_session_and_stopped_producer_before_durable_success(tmp_path):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    rejected = await command(acquisition, AcquisitionCommand.end_recording, "running-end")
    assert not rejected.ok
    await acquisition._on_stop_stream()
    mismatch = await command(acquisition, AcquisitionCommand.end_recording, "wrong-end", "other-session")
    assert not mismatch.ok
    assert manifest(info)["status"] == "recording"
    result = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert result.ok
    assert result.data["id"] == info["id"]
    assert result.data["status"] == manifest(info)["status"] == "completed"
    assert manifest(info)["sample_count"] == 3
    assert manifest(info)["signal_info"]["channels"][0]["name"] == "Cz"
    final_events = acquisition.event_bus.events[-2:]
    assert final_events[0].type is EventType.RawRecordingState
    assert final_events[0].data["status"] == "completed"
    assert final_events[1].data.command_id == "end"
    duplicate = await command(acquisition, AcquisitionCommand.end_recording, "end-again")
    assert duplicate.ok and duplicate.data == result.data


@pytest.mark.asyncio
async def test_pause_and_resume_retain_one_recording_and_sequence(tmp_path):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    await acquisition._on_stop_stream()
    assert acquisition._recording == info
    assert manifest(info)["status"] == "recording"
    revision = "b" * 64
    updated = await command(acquisition, AcquisitionCommand.update_revision, "update",
                            generation="generation-1", expected_revision="revision-1", target_revision=revision)
    assert updated.ok
    await stream_one(acquisition)
    await acquisition._on_stop_stream()
    assert acquisition._recording == info
    result = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert result.ok
    chunks = list(iter_recorded_chunks(info["path"]))
    assert [value.sequence_id for value in chunks] == [0, 1]
    assert manifest(info)["chunk_count"] == 2
    entries = [json.loads(line) for line in (Path(info["path"]) / "chunks.jsonl").read_text().splitlines()]
    assert [entry["config_revision"] for entry in entries] == ["revision-1", revision]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["shutdown", "disconnect"])
async def test_shutdown_and_disconnect_finish_as_interrupted(tmp_path, operation):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    if operation == "shutdown":
        await acquisition.shutdown()
    else:
        result = await command(acquisition, AcquisitionCommand.disconnect, "disconnect")
        assert result.ok
    assert acquisition.fetch_task is None
    assert acquisition._recording is None
    assert manifest(info)["status"] == "interrupted"
    assert len(list(iter_recorded_chunks(info["path"]))) == 1
    result = await command(acquisition, AcquisitionCommand.end_recording, "late-end")
    assert not result.ok


@pytest.mark.asyncio
async def test_raw_write_failure_prevents_forwarding_and_successful_drain(tmp_path, monkeypatch):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)

    def disk_full(*args, **kwargs):
        raise OSError("raw disk full")

    monkeypatch.setattr(recording_module.np, "save", disk_full)
    acquisition.data_source.chunks.put_nowait(raw())
    await acquisition._on_start_stream()
    await acquisition.fetch_task
    assert acquisition.data_manager.items == []
    state = next(event.data for event in reversed(acquisition.event_bus.events)
                 if event.type is EventType.RawRecordingState)
    assert state["status"] == "failed"
    assert state["session_id"] == "session-1"
    result = await command(acquisition, AcquisitionCommand.stop_and_drain, "drain")
    assert not result.ok
    assert not any(isinstance(value, StreamBarrier) for value in acquisition.data_manager.items)
    result = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert not result.ok
    await acquisition.shutdown()
    assert manifest(info)["status"] == "failed"


@pytest.mark.asyncio
async def test_flush_failure_prevents_barrier_and_cannot_be_retried_as_success(tmp_path, monkeypatch):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    original = recording_module.os.fsync
    fail = True

    def fsync(fd):
        nonlocal fail
        if fail:
            fail = False
            raise OSError("raw flush failed")
        original(fd)

    monkeypatch.setattr(recording_module.os, "fsync", fsync)
    result = await command(acquisition, AcquisitionCommand.stop_and_drain, "drain")
    assert not result.ok
    assert acquisition.fetch_task is None
    retry = await command(acquisition, AcquisitionCommand.stop_and_drain, "drain-again")
    assert not retry.ok
    assert not any(isinstance(value, StreamBarrier) for value in acquisition.data_manager.items)
    await acquisition.shutdown()
    assert manifest(info)["status"] == "failed"


@pytest.mark.asyncio
async def test_abort_closes_failed_recording_without_claiming_completion(tmp_path, monkeypatch):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    await acquisition._on_stop_stream()
    acquisition._fetch_failure = "disk full"
    result = await command(acquisition, AcquisitionCommand.abort_recording, "abort")
    assert result.ok
    assert result.data["status"] == "failed"
    assert manifest(info)["status"] == "failed"
    repeated = await command(acquisition, AcquisitionCommand.abort_recording, "abort-again")
    assert repeated.ok and repeated.data == result.data
    completed = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert not completed.ok
    await acquisition.shutdown()


@pytest.mark.asyncio
async def test_running_acquisition_periodically_checkpoints_recording(tmp_path):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    acquisition._next_recording_flush = 0
    loop = asyncio.create_task(acquisition.main_loop())
    try:
        await asyncio.sleep(0.03)
        assert manifest(info)["checkpoint"]["chunk_count"] == 1
        assert acquisition.data_source.is_streaming
    finally:
        acquisition.should_exit = True
        await loop
        await acquisition.shutdown()


@pytest.mark.asyncio
async def test_failed_final_fsync_cannot_be_reported_successfully_on_end_retry(tmp_path, monkeypatch):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    await stream_one(acquisition)
    await acquisition._on_stop_stream()
    original = recording_module.os.fsync
    fail = True

    def fsync(fd):
        nonlocal fail
        if fail:
            fail = False
            raise OSError("final flush failed")
        original(fd)

    monkeypatch.setattr(recording_module.os, "fsync", fsync)
    first = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert not first.ok
    assert manifest(info)["status"] == "failed"
    second = await command(acquisition, AcquisitionCommand.end_recording, "end-again")
    assert not second.ok
    await acquisition.shutdown()


@pytest.mark.asyncio
async def test_recording_cannot_be_reassigned_or_reopened_after_completion(tmp_path):
    acquisition = runtime(tmp_path)
    info = await begin(acquisition)
    different = await command(acquisition, AcquisitionCommand.begin_recording, "other", "other-session")
    assert not different.ok
    assert acquisition._recording == info
    same = await command(acquisition, AcquisitionCommand.begin_recording, "same",
                         subject_id="subject-1", paradigm="test-paradigm")
    assert same.ok and same.data == info
    result = await command(acquisition, AcquisitionCommand.end_recording, "end")
    assert result.ok
    reopened = await command(acquisition, AcquisitionCommand.begin_recording, "reopen",
                             subject_id="subject-1", paradigm="test-paradigm")
    assert not reopened.ok
    assert manifest(info)["status"] == "completed"


@pytest.mark.asyncio
async def test_begin_rejects_disconnected_source_without_creating_files(tmp_path):
    acquisition = runtime(tmp_path)
    acquisition.data_source.is_connected = False
    result = await command(acquisition, AcquisitionCommand.begin_recording, "begin")
    assert not result.ok
    assert acquisition._recording is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("preparation_required", [False, True])
async def test_begin_requires_descriptor_for_prepared_sources_only(tmp_path, preparation_required):
    acquisition = runtime(tmp_path)
    acquisition.preparation_required = preparation_required
    result = await command(acquisition, AcquisitionCommand.begin_recording, "begin")
    if preparation_required:
        assert not result.ok
        assert "descriptor" in result.message
        assert list(tmp_path.iterdir()) == []
        await acquisition._on_get_metadata()
        result = await command(acquisition, AcquisitionCommand.begin_recording, "begin-with-descriptor")
    assert result.ok
    document = manifest(result.data)
    assert (document["signal_info"] is not None) is preparation_required
    await command(acquisition, AcquisitionCommand.end_recording, "end")


@pytest.mark.asyncio
async def test_runtime_accepts_alternate_recorder_without_numpy_storage(tmp_path):
    class MemoryRecorder:
        def __init__(self):
            self.chunks = []
            self.calls = []

        def start(self, metadata):
            self.calls.append("start")
            self.metadata = metadata
            self.info = {"id": "test", "session_id": metadata["session_id"], "path": str(tmp_path),
                         "format": "test.memory", "format_version": 7}
            return self.info.copy()

        def append(self, chunk, *, config_revision=None):
            self.calls.append("append")
            self.chunks.append((chunk, config_revision))

        def flush(self):
            self.calls.append("flush")

        def finish(self, status="completed", error=None):
            self.calls.append("finish")
            return {**self.info, "status": status, "chunk_count": len(self.chunks),
                    "sample_count": sum(chunk.raw_data.shape[1] for chunk, _ in self.chunks)}

    writer = MemoryRecorder()
    acquisition = AcquisitionRuntime(
        Source(), queue.Queue(), Sink(), Bus(), source_id="test-source", preparation_required=False,
        config_revision="revision-1", recorder=writer,
    )
    info = await begin(acquisition)
    await stream_one(acquisition)
    await acquisition._on_stop_stream()
    result = await command(acquisition, AcquisitionCommand.end_recording, "end")

    assert result.ok and result.data["status"] == "completed"
    assert result.data["format"] == info["format"] == "test.memory"
    assert result.data["format_version"] == 7
    assert writer.calls == ["start", "append", "flush", "finish"]
    assert writer.metadata["subject_id"] == "subject-1"
    stored, revision = writer.chunks[0]
    np.testing.assert_array_equal(stored.raw_data, raw())
    assert revision == "revision-1"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_runtime_requires_explicit_recorder_for_recording_commands():
    acquisition = AcquisitionRuntime(
        Source(), queue.Queue(), Sink(), Bus(), source_id="test-source", preparation_required=False,
    )
    result = await command(acquisition, AcquisitionCommand.begin_recording, "begin")
    assert not result.ok
    assert "not configured" in result.message
