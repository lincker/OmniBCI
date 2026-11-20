"""File playback resumes exactly at the next sample delivered by acquisition."""
from pathlib import Path
import asyncio
import os
from copy import deepcopy

import numpy as np
import pytest

from omnibci.domain.ports.acquisition import SignalSource, SourceCheckpoint
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader


class FakeAdapter:
    trigger_mode = 'events'
    resume_mode = "restart-only"
    def __init__(self, _path):
        self.data = np.arange(22).reshape(2, 11)
        self.read_calls = []

    async def read_chunk(self, start, end):
        self.read_calls.append((start, end))
        chunk = self.data[:, start:end]
        return chunk if chunk.shape[1] else None

    async def close(self):
        pass

    def get_metadata(self):
        return "fake-file"


@pytest.fixture
def reader_factory(tmp_path, monkeypatch):
    from omnibci.infrastructure.plugins.acquisition.file import adapter

    monkeypatch.setattr(adapter, "CNTReaderAdapter", FakeAdapter)
    path = tmp_path / "source.cnt"
    path.write_bytes(b"sample-file")

    def create(file_path=path):
        return FileReader(parameters={'data_file_path': str(file_path), 'file_reader_chunk_size': 4}, resource_root=Path(__file__).parent)

    return create


def test_resume_delivers_every_sample_once_and_manual_start_resets(reader_factory):
    async def scenario():
        first = reader_factory()
        await first.connect()
        await first.start_stream()
        chunks = [await first.read_chunk(), await first.read_chunk()]
        await first.stop_stream()
        checkpoint = first.checkpoint()
        assert checkpoint.state["next_sample"] == 8
        await first.disconnect()

        resumed = reader_factory()
        await resumed.connect()
        resumed.restore_checkpoint(checkpoint)
        checkpoint.state["next_sample"] = 0
        await resumed.start_stream()
        chunks.append(await resumed.read_chunk())
        assert chunks[-1].shape[1] == 3
        assert resumed.file_pointer == 11
        assert await resumed.read_chunk() is None
        assert resumed.checkpoint().state["next_sample"] == 11
        np.testing.assert_array_equal(np.concatenate(chunks, axis=1), FakeAdapter(None).data)

        await resumed.start_stream()
        assert resumed.file_pointer == 0
        np.testing.assert_array_equal(await resumed.read_chunk(), FakeAdapter(None).data[:, :4])

    asyncio.run(scenario())


@pytest.mark.parametrize("during_adapter_read", [False, True])
def test_cancelled_read_does_not_advance_cursor(reader_factory, monkeypatch, during_adapter_read):
    async def scenario():
        reader = reader_factory()
        await reader.connect()
        await reader.start_stream()
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def pause(*_args, **_kwargs):
            entered.set()
            await blocked.wait()

        if during_adapter_read:
            monkeypatch.setattr(reader.reader_adapter, "read_chunk", pause)
        else:
            monkeypatch.setattr(asyncio, "sleep", pause)
        task = asyncio.create_task(reader.read_chunk())
        await entered.wait()
        await reader.stop_stream()
        with pytest.raises(RuntimeError, match="join"):
            reader.checkpoint()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert reader.file_pointer == 0
        assert reader.checkpoint().state["next_sample"] == 0

    asyncio.run(scenario())


def test_checkpoint_requires_connected_and_stopped_reader(reader_factory):
    async def scenario():
        reader = reader_factory()
        with pytest.raises(ValueError, match="connected"):
            reader.checkpoint()
        await reader.connect()
        checkpoint = reader.checkpoint()
        await reader.start_stream()
        with pytest.raises(RuntimeError, match="Stop"):
            reader.checkpoint()
        with pytest.raises(RuntimeError, match="Stop"):
            reader.restore_checkpoint(checkpoint)

    asyncio.run(scenario())


def test_checkpoint_rejects_different_file(reader_factory, tmp_path):
    async def scenario():
        reader = reader_factory()
        await reader.connect()
        checkpoint = reader.checkpoint()
        other_path = tmp_path / "other.cnt"
        other_path.write_bytes(b"sample-file")
        other = reader_factory(other_path)
        await other.connect()
        with pytest.raises(ValueError, match="differs"):
            other.restore_checkpoint(checkpoint)

    asyncio.run(scenario())


def test_identity_survives_path_alias_and_chunk_size_change(reader_factory):
    from pathlib import Path

    from omnibci.application.source_checkpoint import checkpoint_for_restore

    async def scenario():
        reader = reader_factory()
        await reader.connect()
        await reader.start_stream()
        delivered = await reader.read_chunk()
        await reader.stop_stream()
        previous = reader.checkpoint()
        path = Path(reader.file_path)
        alias = str(path.parent / ".." / path.parent.name / path.name)
        resumed = reader_factory(alias)
        resumed._chunk_size = 2
        await resumed.connect()
        restored = checkpoint_for_restore(previous, resumed.checkpoint())
        assert restored is not None
        resumed.restore_checkpoint(restored)
        await resumed.start_stream()
        next_chunk = await resumed.read_chunk()
        np.testing.assert_array_equal(delivered, FakeAdapter(None).data[:, :4])
        np.testing.assert_array_equal(next_chunk, FakeAdapter(None).data[:, 4:6])

    asyncio.run(scenario())


@pytest.mark.parametrize("after_staging", [False, True])
def test_checkpoint_rejects_changed_file(reader_factory, after_staging):
    async def scenario():
        reader = reader_factory()
        await reader.connect()
        checkpoint = reader.checkpoint()
        if after_staging:
            reader.restore_checkpoint(checkpoint)
        # Same length, changed timestamp: size alone is insufficient identity.
        stat = os.stat(reader.file_path)
        os.utime(reader.file_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        if after_staging:
            with pytest.raises(ValueError, match="changed"):
                await reader.start_stream()
            assert not reader._is_streaming
        else:
            with pytest.raises(ValueError, match="changed"):
                reader.restore_checkpoint(checkpoint)
        with pytest.raises(ValueError, match="changed"):
            reader.checkpoint()

    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [
    ("next_sample", True), ("next_sample", -1), ("next_sample", 12), ("unexpected", {}),
])
def test_checkpoint_rejects_malformed_or_out_of_range_data(reader_factory, field, value):
    async def scenario():
        reader = reader_factory()
        await reader.connect()
        checkpoint = deepcopy(reader.checkpoint())
        checkpoint.state[field] = value
        with pytest.raises(ValueError):
            reader.restore_checkpoint(checkpoint)

    asyncio.run(scenario())


def test_non_checkpoint_sources_use_none_and_reject_restore():
    class LiveSource(SignalSource):
        trigger_mode = "events"
        resume_mode = "live-current"
        async def observe(self): pass
        async def connect(self): return None
        async def start_stream(self): return None
        async def read_chunk(self): return None
        async def stop_stream(self): return None
        async def disconnect(self): return None

    source = LiveSource()
    assert source.checkpoint() is None
    with pytest.raises(NotImplementedError):
        source.restore_checkpoint(SourceCheckpoint(resource_id="file", resource_revision="rev", state={}))
