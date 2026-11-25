"""Unit and integration tests for FileReader lifecycle, checkpoints, and parameters."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import os
from pathlib import Path

import mne
import numpy as np
import pytest

from omnibci.application.source_checkpoint import checkpoint_for_restore
from omnibci.domain.ports.acquisition import SignalSource, SourceCheckpoint
from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader

_CNT_FIXTURE = Path(__file__).parents[1] / "fixture" / "train.cnt"


def _run(coro):
    return asyncio.run(coro)


class _FakeAdapter:
    trigger_mode = "events"
    resume_mode = "restart-only"

    def __init__(self, total_samples: int = 1000, channels: int = 9):
        self.data = np.arange(total_samples * channels).reshape(channels, total_samples)
        self.closed = False
        self.read_calls = []

    async def read_chunk(self, start: int, end: int):
        self.read_calls.append((start, end))
        sl = self.data[:, start:end]
        return sl if sl.shape[1] > 0 else None

    async def close(self):
        self.closed = True

    def get_metadata(self):
        return "fake-device"

    def describe_output(self):
        raise NotImplementedError("Fake adapter has no layout")


class _CheckpointFakeAdapter:
    trigger_mode = "events"
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

    monkeypatch.setattr(adapter, "CNTReaderAdapter", _CheckpointFakeAdapter)
    path = tmp_path / "source.cnt"
    path.write_bytes(b"sample-file")

    def create(file_path=path):
        return FileReader(
            parameters={"data_file_path": str(file_path), "file_reader_chunk_size": 4},
            resource_root=Path(__file__).parent,
        )

    return create


@pytest.mark.parametrize("parameters", [{}, {"data_file_path": ""}, {"data_file_path": "  "}])
def test_reader_requires_an_explicit_path(parameters):
    with pytest.raises(ValueError, match="Select a recording file"):
        FileReader(parameters=parameters, resource_root=Path(__file__).parent)


def test_file_reader_uses_explicit_path_and_chunk_size(tmp_path):
    path = tmp_path / "subject.cnt"
    parameters = {"data_file_path": str(path), "file_reader_chunk_size": 37}

    reader = FileReader(parameters=parameters, resource_root=Path(__file__).parent)
    parameters["file_reader_chunk_size"] = 99

    assert reader.file_path == str(path)
    assert reader._chunk_size == 37
    assert reader.file_pointer == 0
    assert reader.reader_adapter is None
    with pytest.raises(TypeError, match="parameters"):
        FileReader()


class TestFileReaderLifecycle:
    def _reader(self, chunk_size: int = 100) -> FileReader:
        r = FileReader(parameters={"data_file_path": "sample.cnt"}, resource_root=Path(__file__).parent)
        r.reader_adapter = _FakeAdapter()
        r._chunk_size = chunk_size
        return r

    def test_initial_flags(self):
        r = FileReader(parameters={"data_file_path": "sample.cnt"}, resource_root=Path(__file__).parent)
        assert r.reader_adapter is None
        assert r._is_streaming is False
        assert r.file_pointer == 0

    def test_connect_and_disconnect_acquire_and_release_adapter(self, monkeypatch, tmp_path):
        from omnibci.infrastructure.plugins.acquisition.file import adapter

        monkeypatch.setattr(adapter, "CNTReaderAdapter", lambda _: _FakeAdapter())
        file_path = tmp_path / "sample.cnt"
        file_path.touch()

        r = FileReader(parameters={"data_file_path": str(file_path)}, resource_root=Path(__file__).parent)

        assert _run(r.connect()) is None
        assert r.reader_adapter is not None
        assert _run(r.disconnect()) is None
        assert r.reader_adapter is None

    def test_start_stream_resets_pointer_and_sets_flag(self):
        r = self._reader()
        r.file_pointer = 999
        assert _run(r.start_stream()) is None
        assert r.file_pointer == 0
        assert r._is_streaming is True

    def test_start_stream_requires_adapter(self):
        r = FileReader(parameters={"data_file_path": "sample.cnt"}, resource_root=Path(__file__).parent)
        r.reader_adapter = None
        with pytest.raises(ValueError):
            _run(r.start_stream())

    def test_read_chunk_advances_pointer_by_chunk_size(self):
        r = self._reader(chunk_size=100)
        _run(r.start_stream())
        _run(r.read_chunk())
        assert r.file_pointer == 100
        _run(r.read_chunk())
        assert r.file_pointer == 200

    def test_read_chunk_requests_correct_window(self):
        r = self._reader(chunk_size=50)
        _run(r.start_stream())
        _run(r.read_chunk())
        assert r.reader_adapter.read_calls[0] == (0, 50)

    def test_read_past_end_stops_stream_and_returns_none(self):
        r = self._reader(chunk_size=100)
        r.reader_adapter = _FakeAdapter(total_samples=100)
        _run(r.start_stream())
        _run(r.read_chunk())
        assert _run(r.read_chunk()) is None
        assert r._is_streaming is False

    def test_stop_stream_clears_flag(self):
        r = self._reader()
        _run(r.start_stream())
        assert _run(r.stop_stream()) is None
        assert r._is_streaming is False

    def test_disconnect_closes_adapter(self):
        r = self._reader()
        adapter = r.reader_adapter
        _run(r.disconnect())
        assert adapter.closed is True
        assert r.reader_adapter is None
        _run(r.disconnect())

    def test_observation_returns_adapter_metadata_without_a_layout(self):
        r = self._reader()
        assert _run(r.observe()).native_text == "fake-device"


def test_cnt_metadata_preserves_native_info_before_output_channel_selection(monkeypatch):
    from omnibci.infrastructure.plugins.acquisition.file.adapter import CNTReaderAdapter

    raw = mne.io.RawArray(np.zeros((2, 10)), mne.create_info(["C3", "BadChannel"], 500, "eeg"))
    raw.info["bads"] = ["BadChannel"]
    native_information = f"{raw.info}\n\nChannel labels: C3, BadChannel\n\n{raw.annotations}"
    monkeypatch.setattr(mne.io, "read_raw_cnt", lambda **kwargs: raw)
    adapter = CNTReaderAdapter("sample.cnt")

    assert adapter.get_metadata() == native_information
    assert "BadChannel" in adapter.get_metadata()
    assert "signal_channel_count" not in adapter.get_metadata()
    descriptor = adapter.describe_output()
    assert [channel.name for channel in descriptor.channels] == ["C3"]
    assert descriptor.output_row_count == adapter.data.shape[0] == 2
    assert descriptor.sample_rate == 500

    adapter._metadata = "Native information is display text, not structured input."
    assert adapter.describe_output() == descriptor


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
        np.testing.assert_array_equal(np.concatenate(chunks, axis=1), _CheckpointFakeAdapter(None).data)

        await resumed.start_stream()
        assert resumed.file_pointer == 0
        np.testing.assert_array_equal(await resumed.read_chunk(), _CheckpointFakeAdapter(None).data[:, :4])

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
        np.testing.assert_array_equal(delivered, _CheckpointFakeAdapter(None).data[:, :4])
        np.testing.assert_array_equal(next_chunk, _CheckpointFakeAdapter(None).data[:, 4:6])

    asyncio.run(scenario())


@pytest.mark.parametrize("after_staging", [False, True])
def test_checkpoint_rejects_changed_file(reader_factory, after_staging):
    async def scenario():
        reader = reader_factory()
        await reader.connect()
        checkpoint = reader.checkpoint()
        if after_staging:
            reader.restore_checkpoint(checkpoint)
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


@pytest.mark.integration
def test_real_cnt_lifecycle_and_first_chunk():
    async def scenario():
        reader = FileReader(parameters={"data_file_path": str(_CNT_FIXTURE)}, resource_root=Path(__file__).parent)

        try:
            assert await reader.connect() is None
            assert reader.reader_adapter is not None
            assert type(reader.reader_adapter).__name__ == "CNTReaderAdapter"
            metadata = await reader.observe()
            assert isinstance(metadata.native_text, str)
            assert "sfreq: 1000.0 Hz" in metadata.native_text
            assert "Annotations" in metadata.native_text
            descriptor = metadata.signal_info
            assert descriptor.signal_channel_count == 9
            assert descriptor.output_row_count == 10
            assert descriptor.sample_rate == 1000
            assert [channel.unit for channel in descriptor.channels] == ["V"] * 9
            assert metadata.native_event == "annotations"
            assert metadata.provenance == "file-reported"
            assert reader.file_pointer == 0

            assert await reader.start_stream() is None
            assert reader._is_streaming is True

            chunk = await reader.read_chunk()
            assert chunk.shape == (10, 100)
            assert np.isfinite(chunk).all()
            assert reader.file_pointer == 100
            with pytest.raises(RuntimeError, match="Stop"):
                await reader.observe()

            assert await reader.stop_stream() is None
            assert reader._is_streaming is False
        finally:
            if reader._is_streaming:
                await reader.stop_stream()
            if reader.reader_adapter is not None:
                await reader.disconnect()

        assert reader.reader_adapter is None

    asyncio.run(scenario())
