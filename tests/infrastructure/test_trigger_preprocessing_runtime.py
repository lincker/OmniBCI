"""Trigger preprocessing belongs to the source stream, before delivery and preview."""

import asyncio
import logging
import pickle
from dataclasses import replace
from queue import Queue
from types import SimpleNamespace

import numpy as np
import pytest

from omnibci.application.events import CommandRequest, StreamBarrier
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
from omnibci.infrastructure.acquisition.preprocessing import TriggerEdgePreprocessor
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader


class LiveSource:
    trigger_is_level = True

    def __init__(self):
        self.is_connected = False
        self.is_streaming = False
        self.chunks = asyncio.Queue()

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_stream(self):
        self.is_streaming = True

    async def stop_stream(self):
        self.is_streaming = False

    async def read_chunk(self):
        return await self.chunks.get()


def runtime(source):
    data, frames = DataManager(Queue()), Queue()
    acquisition = AcquisitionRuntime(
        source, Queue(), data, source_id="source", preparation_required=False,
        generation="generation", config_revision="revision", preview_queue=frames,
    )
    return acquisition, data, frames


def samples(triggers):
    return np.vstack((np.arange(len(triggers)), triggers)).astype(np.float32)


async def receive(data):
    async with asyncio.timeout(2):
        while data.empty():
            await asyncio.sleep(0)
    return data.get_data()


@pytest.mark.asyncio
async def test_level_triggers_are_normalized_across_chunks_before_delivery_and_preview():
    source = LiveSource()
    acquisition, data, frames = runtime(source)
    original = samples([0, 7, 7, 7, 8, 8, 0, 8])
    snapshot = original.copy()
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        for start, end in ((0, 3), (3, 6), (6, 8)):
            await source.chunks.put(original[:, start:end])
        delivered = [await receive(data) for _ in range(3)]
        normalized = np.concatenate([chunk.raw_data for chunk in delivered], axis=1)
        np.testing.assert_array_equal(normalized[-1], [0, 7, 0, 0, 8, 0, 0, 8])
        np.testing.assert_array_equal(normalized[:-1], snapshot[:-1])
        np.testing.assert_array_equal(original, snapshot)
        assert [chunk.sequence_id for chunk in delivered] == [0, 1, 2]
        for chunk in delivered:
            frame = pickle.loads(frames.get_nowait())
            np.testing.assert_array_equal(frame["values"], chunk.raw_data)
            assert frame["sequence_id"] == chunk.sequence_id
            assert frame["timestamp_ns"] == str(chunk.timestamp)
            assert chunk.raw_data.dtype == original.dtype

        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain")))
        barrier = data.get_data()
        assert isinstance(barrier, StreamBarrier)
        assert barrier.command_id == "drain" and barrier.last_sequence == 2
        assert data.empty()
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
async def test_repeated_start_and_connect_while_running_do_not_reset_trigger_state():
    source = LiveSource()
    acquisition, data, _ = runtime(source)
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await source.chunks.put(samples([7, 7]))
        np.testing.assert_array_equal((await receive(data)).triggers, [7, 0])
        task, connection = acquisition.fetch_task, acquisition.connection_id

        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await source.chunks.put(samples([7, 8, 8]))

        np.testing.assert_array_equal((await receive(data)).triggers, [0, 8, 0])
        assert acquisition.fetch_task is task and acquisition.connection_id == connection
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", ["stop_start", "reconnect", "eof"])
async def test_a_new_stream_keeps_its_first_nonzero_trigger(restart):
    source = LiveSource()
    acquisition, data, _ = runtime(source)
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        await source.chunks.put(samples([7, 7]))
        np.testing.assert_array_equal((await receive(data)).triggers, [7, 0])
        old_connection = acquisition.connection_id

        if restart == "reconnect":
            await acquisition._on_disconnect()
            await acquisition._on_connect()
            assert acquisition.connection_id != old_connection
        elif restart == "eof":
            await source.chunks.put(None)
            await acquisition.fetch_task
        else:
            await acquisition._on_stop_stream()

        await acquisition._on_start_stream()
        await source.chunks.put(samples([7, 7]))
        chunk = await receive(data)
        np.testing.assert_array_equal(chunk.triggers, [7, 0])
        assert chunk.sequence_id == 1
        if restart != "reconnect":
            assert acquisition.connection_id == old_connection
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
async def test_discrete_file_events_and_cached_views_survive_replay_unchanged():
    original = samples([7, 7, 8, 8, 0, 7])
    snapshot = original.copy()
    source = FileReader(parameters={"data_file_path": "events.cnt", "file_reader_chunk_size": 2})

    async def read_chunk(*, start, end):
        return original[:, start:end] if start < original.shape[1] else None

    source.reader_adapter = SimpleNamespace(read_chunk=read_chunk)
    source._is_connected = True
    acquisition, data, frames = runtime(source)
    try:
        for _ in range(2):
            await acquisition._on_start_stream()
            await acquisition.fetch_task
            chunks = [data.get_data() for _ in range(3)]
            np.testing.assert_array_equal(np.concatenate([item.raw_data for item in chunks], axis=1), snapshot)
            for chunk in chunks:
                np.testing.assert_array_equal(pickle.loads(frames.get_nowait())["values"], chunk.raw_data)
            assert data.empty()
        np.testing.assert_array_equal(original, snapshot)
    finally:
        await acquisition._on_stop_stream()


@pytest.mark.asyncio
@pytest.mark.parametrize("declaration", [None, False, "true"])
async def test_sources_without_an_explicit_level_declaration_keep_events(declaration):
    source = LiveSource()
    source.trigger_is_level = declaration
    acquisition, data, _ = runtime(source)
    try:
        await acquisition._on_connect()
        await acquisition._on_start_stream()
        original = samples([7, 7, 8, 8])
        await source.chunks.put(original)
        np.testing.assert_array_equal((await receive(data)).raw_data, original)
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
async def test_additional_steps_run_in_order_before_both_consumers_and_reset_on_restart():
    class SignalTransform:
        def __init__(self, scale, offset):
            self.scale, self.offset = scale, offset
            self.resets = 0

        def reset(self):
            self.resets += 1

        def process(self, chunk):
            raw = chunk.raw_data.copy()
            raw[:-1] = raw[:-1] * self.scale + self.offset
            return replace(chunk, raw_data=raw)

    source = LiveSource()
    acquisition, data, frames = runtime(source)
    add, scale = SignalTransform(1, 3), SignalTransform(2, 0)
    acquisition._preprocessors = (add, TriggerEdgePreprocessor(), scale)
    try:
        await acquisition._on_connect()
        for _ in range(2):
            resets_before_start = add.resets, scale.resets
            await acquisition._on_start_stream()
            original = samples([7, 7])
            await source.chunks.put(original)
            delivered = await receive(data)
            np.testing.assert_array_equal(delivered.data, [[6, 8]])
            np.testing.assert_array_equal(delivered.triggers, [7, 0])
            np.testing.assert_array_equal(original, samples([7, 7]))
            np.testing.assert_array_equal(pickle.loads(frames.get_nowait())["values"], delivered.raw_data)
            assert (add.resets, scale.resets) == tuple(count + 1 for count in resets_before_start)
            await acquisition._on_stop_stream()
    finally:
        await acquisition.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("level_triggers,steps", [(True, "TriggerEdgePreprocessor"), (False, "passthrough")])
async def test_preprocessing_lifecycle_logs_report_resets_and_passthrough(caplog, level_triggers, steps):
    source = LiveSource()
    source.trigger_is_level = level_triggers
    acquisition, _, _ = runtime(source)
    caplog.set_level(logging.INFO, logger="omnibci.infrastructure.acquisition")
    try:
        await acquisition._on_connect()
        connection = acquisition.connection_id
        await acquisition._on_start_stream()
        await acquisition._on_start_stream()
        await acquisition._on_stop_stream()
        await acquisition._on_start_stream()
        await acquisition._on_disconnect()
        await acquisition._on_connect()
        messages = [record.getMessage() for record in caplog.records
                    if record.getMessage().startswith("Acquisition preprocessing:")]
        assert len(messages) == 4
        for message, reason in zip(messages, ("connect", "stream_start", "stream_start", "connect"), strict=True):
            assert f"reason={reason}" in message
            assert "source_id=source generation=generation" in message
            assert f"steps={steps}" in message
        assert all(f"connection_id={connection}" in message for message in messages[:3])
        assert f"connection_id={acquisition.connection_id}" in messages[-1]
        assert acquisition.connection_id != connection
    finally:
        await acquisition.shutdown()
