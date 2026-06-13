"""
Characterization tests — Acquisition 限界上下文.

锁定采集上下文当前的两个稳定接缝：

  1. 插件自注册机制（Registerable + ObjectFactory）：FileReader 以
     plugin_type='file_reader' 注册进 SignalSource 的注册表，工厂按
     settings 中的 ACQUISITION.signal_source 解析出具体实现。这是采集层
     "换设备只改配置" 能力的根基，重构时绝不能破坏。

  2. SignalSource 端口生命周期：FileReader 的 connect → start_stream →
     read_chunk（文件指针按 chunk_size 步进）→ stop_stream 的可观察行为。
     用 fake adapter 注入，避免依赖真实 .cnt 文件与 mne。

说明：read_chunk 等为协程，这里用 asyncio.run 驱动，避免引入
pytest-asyncio 依赖。
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from omnibci.domain.ports.acquisition import SignalSource
from omnibci.shared.factory import ObjectFactory
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

    def get_name(self):
        return "fake-device"


# ---------------------------------------------------------------------------
# Plugin registry / factory mechanism
# ---------------------------------------------------------------------------

class TestDataSourceRegistry:
    def test_filereader_is_registered_under_its_plugin_type(self):
        assert "file_reader" in SignalSource._target_registry
        assert SignalSource._target_registry["file_reader"] is FileReader

    def test_factory_resolves_configured_data_source(self):
        # settings.toml ships ACQUISITION.signal_source = "file_reader"
        instance = ObjectFactory.create(SignalSource, "ACQUISITION.signal_source")
        assert isinstance(instance, FileReader)

    def test_factory_returns_none_for_unknown_type(self, monkeypatch):
        import omnibci.shared.factory as factory_mod
        monkeypatch.setattr(factory_mod.settings, "get", lambda key: "does_not_exist")
        assert ObjectFactory.create(SignalSource, "ACQUISITION.signal_source") is None


# ---------------------------------------------------------------------------
# FileReader port lifecycle
# ---------------------------------------------------------------------------

class TestFileReaderLifecycle:
    def _reader(self, chunk_size: int = 100) -> FileReader:
        r = FileReader()
        r.reader_adapter = _FakeAdapter()
        r._chunk_size = chunk_size
        return r

    def test_initial_flags(self):
        r = FileReader()
        assert r._is_streaming is False
        assert r.file_pointer == 0

    def test_start_stream_resets_pointer_and_sets_flag(self):
        r = self._reader()
        r.file_pointer = 999
        assert _run(r.start_stream()) is True
        assert r.file_pointer == 0
        assert r._is_streaming is True

    def test_start_stream_requires_adapter(self):
        r = FileReader()
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

    def test_get_metadata_returns_device_name(self):
        r = self._reader()
        assert _run(r.get_metadata()) == "fake-device"

    def test_set_speed_rejects_non_positive(self):
        r = FileReader()
        with pytest.raises(ValueError):
            r.set_speed(0)
