"""
Characterization tests — Acquisition 限界上下文.

锁定采集上下文当前的两个稳定接缝：

  1. 插件注册机制（register_plugin 装饰器 + ObjectFactory）：FileReader 以
     plugin_type='file_reader' 注册进 SignalSource 的注册表，调用方读
     settings 中的 ACQUISITION.signal_source 后由工厂解析出具体实现。这是
     采集层 "换设备只改配置" 能力的根基，重构时绝不能破坏。
     （P0-3：原 Registerable 元类自注册已改为显式装饰器，注册表与工厂
     迁至 infrastructure/plugins；工厂签名从 config_key 改为 target_id。）

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
from omnibci.infrastructure.plugins.registry import ObjectFactory, registry_for
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
# Plugin registry / factory mechanism
# ---------------------------------------------------------------------------

class TestDataSourceRegistry:
    def test_filereader_is_registered_under_its_plugin_type(self):
        assert "file_reader" in registry_for(SignalSource)
        assert registry_for(SignalSource)["file_reader"] is FileReader

    def test_factory_resolves_configured_data_source(self):
        # settings.toml ships ACQUISITION.signal_source = "file_reader"
        from omnibci.conf import settings
        instance = ObjectFactory.create(SignalSource, settings.get("ACQUISITION.signal_source"))
        assert isinstance(instance, FileReader)

    def test_factory_returns_none_for_unknown_type(self):
        assert ObjectFactory.create(SignalSource, "does_not_exist") is None

    def test_factory_returns_none_for_missing_target(self):
        # 配置读取上移后，"缺配置" 在工厂侧表现为 target_id=None
        assert ObjectFactory.create(SignalSource, None) is None


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

        r = FileReader()
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

    def test_get_metadata_returns_adapter_metadata(self):
        r = self._reader()
        assert _run(r.get_metadata()) == "fake-device"

    def test_set_speed_rejects_non_positive(self):
        r = FileReader()
        with pytest.raises(ValueError):
            r.set_speed(0)
