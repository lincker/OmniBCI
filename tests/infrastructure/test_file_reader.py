"""Unit tests for FileReader lifecycle."""
from __future__ import annotations

import asyncio

import mne
import numpy as np
import pytest

from omnibci.infrastructure.plugins.acquisition.file.reader import FileReader


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake adapter — stands in for CNTReaderAdapter (no mne / no real file)
# ---------------------------------------------------------------------------

class _FakeAdapter:
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


# ---------------------------------------------------------------------------
# FileReader port lifecycle
# ---------------------------------------------------------------------------

class TestFileReaderLifecycle:
    def _reader(self, chunk_size: int = 100) -> FileReader:
        r = FileReader(parameters={"data_file_path": "sample.cnt"})
        r.reader_adapter = _FakeAdapter()
        r._chunk_size = chunk_size
        return r

    def test_initial_flags(self):
        r = FileReader(parameters={"data_file_path": "sample.cnt"})
        assert r.is_connected is False
        assert r._is_streaming is False
        assert r.file_pointer == 0

    def test_connect_and_disconnect_update_public_connected_state(
        self, monkeypatch, tmp_path
    ):
        from omnibci.infrastructure.plugins.acquisition.file import adapter

        monkeypatch.setattr(adapter, "CNTReaderAdapter", lambda _: _FakeAdapter())
        file_path = tmp_path / "sample.cnt"
        file_path.touch()

        r = FileReader(parameters={"data_file_path": str(file_path)})
        r.set_file_path(str(file_path))

        assert _run(r.connect()) is True
        assert r.is_connected is True
        assert _run(r.disconnect()) is True
        assert r.is_connected is False

    def test_start_stream_resets_pointer_and_sets_flag(self):
        r = self._reader()
        r.file_pointer = 999
        assert _run(r.start_stream()) is True
        assert r.file_pointer == 0
        assert r._is_streaming is True

    def test_start_stream_requires_adapter(self):
        r = FileReader(parameters={"data_file_path": "sample.cnt"})
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
        # B2 fix: EOF is a clean end-of-stream signal (None), not an exception.
        r = self._reader(chunk_size=100)
        r.reader_adapter = _FakeAdapter(total_samples=100)
        _run(r.start_stream())
        _run(r.read_chunk())            # consumes [0:100]
        assert _run(r.read_chunk()) is None   # [100:200] → empty → None
        assert r._is_streaming is False

    def test_stop_stream_clears_flag(self):
        r = self._reader()
        _run(r.start_stream())
        assert _run(r.stop_stream()) is True
        assert r._is_streaming is False

    def test_disconnect_closes_adapter(self):
        r = self._reader()
        _run(r.disconnect())
        assert r.reader_adapter.closed is True

    def test_get_metadata_returns_adapter_metadata(self):
        r = self._reader()
        assert _run(r.get_metadata()) == "fake-device"


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
    descriptor = adapter.describe_output(source_id="file_reader", connection_id="connection")
    assert [channel.name for channel in descriptor.channels] == ["C3"]
    assert descriptor.output_row_count == adapter.data.shape[0] == 2
    assert descriptor.sample_rate == 500

    adapter._metadata = "Native information is display text, not structured input."
    assert adapter.describe_output(source_id="file_reader", connection_id="connection") == descriptor
