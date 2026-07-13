"""
Characterization tests — Acquisition 领域服务（命令/状态机）.

补齐采集上下文此前的测试缺口：`tests/characterization/test_acquisition_characterization.py`
只锁了插件注册表 + `FileReader` 端口生命周期，但真正编排采集子进程行为的
`Acquisition` 领域服务（`infrastructure/acquisition/acquisition.py`）一直没有单元级
安全网——它的命令分派、fetch 循环、shutdown 都只在需要真实 mne / 子进程的集成测试
里被间接触达。

本组用注入的 fake data_source / queue / data_manager 把 `Acquisition` 当前的
**可观察行为**钉死，使后续对 `execute()` 的结构收口、端口属性化（消除
`_is_connected` 私有穿透）等重构有红线可依。

只依赖 numpy + asyncio，可在纯 numpy 环境用独立 runner 跑通（无需 pytest 插件、
无需 tomlkit / mne）。
"""
from __future__ import annotations

import asyncio

import numpy as np

from omnibci.infrastructure.acquisition.acquisition import (
    Acquisition,
    AcquisitionCommand,
    AcquisitionState,
)
from omnibci.domain.signal import SignalChunk


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes — stand in for a SignalSource plugin, the command queue and the
# shared DataManager. The data source mirrors the *post-hardening* port shape
# (public `is_connected` property backed by `_is_connected`), so the same
# tests stay green whether shutdown() reads the private flag or the property.
# ---------------------------------------------------------------------------

class _FakeDataSource:
    def __init__(self, chunks=None, fail_after=None):
        self.calls = []
        self._is_connected = False
        self._is_streaming = False
        self._chunks = list(chunks) if chunks is not None else []
        self._read_idx = 0
        # number of successful read_chunk() calls after which to raise
        # CancelledError, so fetch()'s loop terminates deterministically.
        self._fail_after = fail_after
        self._reads = 0

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    async def connect(self) -> bool:
        self.calls.append("connect")
        self._is_connected = True
        return True

    async def disconnect(self) -> bool:
        self.calls.append("disconnect")
        self._is_connected = False
        return True

    async def start_stream(self) -> bool:
        self.calls.append("start_stream")
        self._is_streaming = True
        return True

    async def stop_stream(self) -> bool:
        self.calls.append("stop_stream")
        self._is_streaming = False
        return True

    async def read_chunk(self):
        if self._fail_after is not None and self._reads >= self._fail_after:
            raise asyncio.CancelledError
        self._reads += 1
        if self._read_idx < len(self._chunks):
            chunk = self._chunks[self._read_idx]
            self._read_idx += 1
            return chunk
        return np.zeros((2, 3))

    async def get_metadata(self):
        self.calls.append("get_metadata")
        return "fake-metadata"


class _FakeQueue:
    """Minimal mp.Queue stand-in (only the surface main_loop touches)."""

    def __init__(self, items=None):
        self._items = list(items) if items else []

    def empty(self) -> bool:
        return not self._items

    def get(self):
        return self._items.pop(0)

    def put(self, item) -> None:
        self._items.append(item)


class _FakeDataManager:
    def __init__(self):
        self.pushed = []

    def put_data(self, chunk) -> None:
        self.pushed.append(chunk)


def _acq(data_source=None, queue=None, data_manager=None) -> Acquisition:
    return Acquisition(
        data_source=data_source or _FakeDataSource(),
        command_queue=queue or _FakeQueue(),
        data_manager=data_manager or _FakeDataManager(),
    )


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

class TestAcquisitionCommandDispatch:
    def test_initial_status_is_stop(self):
        assert _acq().status is AcquisitionState.stop

    def test_connect_connects_source_and_sets_running(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.connect))
        assert a.data_source.calls == ["connect"]
        assert a.status is AcquisitionState.running

    def test_disconnect_disconnects_source_and_sets_stop(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.connect))
        _run(a.execute(AcquisitionCommand.disconnect))
        assert a.data_source.calls == ["connect", "disconnect"]
        assert a.status is AcquisitionState.stop

    def test_start_stream_when_running_starts_source_and_creates_fetch_task(self):
        async def scenario():
            a = _acq(data_source=_FakeDataSource(fail_after=0))
            await a.execute(AcquisitionCommand.connect)
            await a.execute(AcquisitionCommand.start_stream)
            assert "start_stream" in a.data_source.calls
            assert a.fetch_task is not None
            a.fetch_task.cancel()  # housekeeping so the loop tears down cleanly
        _run(scenario())

    def test_start_stream_while_not_running_is_ignored(self):
        # LOCKED QUIRK: start_stream is gated on status == running. When the
        # source was never connected the command falls through every branch
        # and is silently dropped (logged as "Unknown"): no start_stream call,
        # no fetch task, status unchanged.
        a = _acq()
        _run(a.execute(AcquisitionCommand.start_stream))
        assert a.data_source.calls == []
        assert a.fetch_task is None
        assert a.status is AcquisitionState.stop

    def test_stop_stream_stops_source_and_cancels_fetch_task(self):
        async def scenario():
            a = _acq(data_source=_FakeDataSource(fail_after=0))
            await a.execute(AcquisitionCommand.connect)
            await a.execute(AcquisitionCommand.start_stream)
            task = a.fetch_task
            await a.execute(AcquisitionCommand.stop_stream)
            await asyncio.sleep(0)  # let the cancellation propagate one loop turn
            assert "stop_stream" in a.data_source.calls
            assert task.cancelled() or task.done()
        _run(scenario())

    def test_stop_acquisition_sets_should_exit(self):
        a = _acq()
        assert a.should_exit is False
        _run(a.execute(AcquisitionCommand.stop_acquisition))
        assert a.should_exit is True

    def test_get_metadata_queries_source(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.get_metadata))
        assert a.data_source.calls == ["get_metadata"]


# ---------------------------------------------------------------------------
# fetch() loop
# ---------------------------------------------------------------------------

class TestAcquisitionFetch:
    def test_fetch_wraps_chunks_into_signalchunk_with_increasing_sequence(self):
        chunks = [np.ones((2, 3)) * i for i in range(3)]
        dm = _FakeDataManager()
        a = _acq(
            data_source=_FakeDataSource(chunks=chunks, fail_after=3),
            data_manager=dm,
        )
        _run(a.fetch())
        assert len(dm.pushed) == 3
        assert all(isinstance(c, SignalChunk) for c in dm.pushed)
        assert [c.sequence_id for c in dm.pushed] == [0, 1, 2]
        assert all(c.timestamp > 0 for c in dm.pushed)

    def test_fetch_stops_silently_on_cancel(self):
        dm = _FakeDataManager()
        a = _acq(data_source=_FakeDataSource(fail_after=0), data_manager=dm)
        _run(a.fetch())  # raises CancelledError on first read → swallowed
        assert dm.pushed == []


# ---------------------------------------------------------------------------
# shutdown()
# ---------------------------------------------------------------------------

class TestAcquisitionShutdown:
    def test_shutdown_disconnects_when_connected(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.connect))
        _run(a.shutdown())
        assert a.data_source.calls == ["connect", "disconnect"]

    def test_shutdown_skips_disconnect_when_not_connected(self):
        a = _acq()
        _run(a.shutdown())
        assert a.data_source.calls == []


# ---------------------------------------------------------------------------
# Standalone runner — lets this file run under a bare numpy environment
# (no pytest plugin), mirroring tests/characterization/README.md convention.
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import traceback

    passed = failed = 0
    for cls in (
        TestAcquisitionCommandDispatch,
        TestAcquisitionFetch,
        TestAcquisitionShutdown,
    ):
        inst = cls()
        for name in sorted(n for n in dir(inst) if n.startswith("test_")):
            try:
                getattr(inst, name)()
                passed += 1
                print(f"PASS {cls.__name__}.{name}")
            except Exception:
                failed += 1
                print(f"FAIL {cls.__name__}.{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
