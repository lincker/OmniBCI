"""Unit tests for Acquisition command dispatch, streaming, and state projection."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from omnibci.infrastructure.acquisition.acquisition import (
    AcquisitionRuntime,
    AcquisitionCommand,
    AcquisitionCommandRejected,
)
from omnibci.application.events import (
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)
from omnibci.domain.signal import SignalChunk
from omnibci.shared.events import EventType


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
        return "Fake source"


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


def _acq(data_source=None, queue=None, data_manager=None) -> AcquisitionRuntime:
    return AcquisitionRuntime(
        data_source=data_source or _FakeDataSource(),
        command_queue=queue or _FakeQueue(),
        data_manager=data_manager or _FakeDataManager(),
        source_id="test-source",
        preparation_required=False,
    )


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

class TestAcquisitionCommandDispatch:
    def test_initial_source_is_disconnected_and_not_streaming(self):
        a = _acq()
        assert a.data_source.is_connected is False
        assert a.data_source.is_streaming is False
        assert a.fetch_task is None

    def test_connect_connects_source_without_starting_stream(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.connect))
        assert a.data_source.calls == ["connect"]
        assert a.data_source.is_connected is True
        assert a.data_source.is_streaming is False
        assert a.fetch_task is None

    def test_disconnect_disconnects_source(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.connect))
        _run(a.execute(AcquisitionCommand.disconnect))
        assert a.data_source.calls == ["connect", "disconnect"]
        assert a.data_source.is_connected is False
        assert a.data_source.is_streaming is False
        assert a.fetch_task is None

    def test_disconnect_stops_stream_and_joins_fetch_task_before_success(self):
        async def scenario():
            source = _FakeDataSource(fail_after=0)
            a = _acq(data_source=source)
            await a.execute(AcquisitionCommand.connect)
            await a.execute(AcquisitionCommand.start_stream)
            task = a.fetch_task

            await a.execute(AcquisitionCommand.disconnect)

            assert source.calls == [
                "connect",
                "start_stream",
                "stop_stream",
                "disconnect",
            ]
            assert task.done()
            assert a.fetch_task is None
            assert source.is_connected is False
            assert source.is_streaming is False

        _run(scenario())

    def test_start_stream_when_connected_starts_source_and_creates_fetch_task(self):
        async def scenario():
            a = _acq(data_source=_FakeDataSource(fail_after=0))
            await a.execute(AcquisitionCommand.connect)
            await a.execute(AcquisitionCommand.start_stream)
            assert "start_stream" in a.data_source.calls
            assert a.data_source.is_streaming is True
            assert a.fetch_task is not None
            a.fetch_task.cancel()  # housekeeping so the loop tears down cleanly
        _run(scenario())

    def test_start_stream_while_disconnected_is_ignored(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.start_stream))
        assert a.data_source.calls == []
        assert a.fetch_task is None
        assert a.data_source.is_connected is False
        assert a.data_source.is_streaming is False

    def test_confirmed_start_stream_rejects_when_not_connected(self):
        bus = _SpyBus()
        a = AcquisitionRuntime(
            data_source=_FakeDataSource(),
            command_queue=_FakeQueue(),
            data_manager=_FakeDataManager(),
            event_bus=bus,
            source_id="test-source",
            preparation_required=False,
        )

        _run(a.execute((
            AcquisitionCommand.start_stream,
            CommandRequest(command_id="cmd-1"),
        )))

        results = [e.data for e in bus.events if e.type == EventType.CommandResult]
        assert results == [CommandResult(
            command_id="cmd-1",
            command="StartStream",
            ok=False,
            message="Cannot start stream before the data source is connected",
            error_code="DATA_SOURCE_NOT_CONNECTED",
            failure_kind=CommandFailureKind.REJECTED,
        )]

    def test_confirmed_connect_emits_state_before_success(self):
        bus = _SpyBus()
        a = AcquisitionRuntime(
            data_source=_FakeDataSource(),
            command_queue=_FakeQueue(),
            data_manager=_FakeDataManager(),
            event_bus=bus,
            source_id="test-source",
            preparation_required=False,
        )

        _run(a.execute((
            AcquisitionCommand.connect,
            CommandRequest(command_id="cmd-2"),
        )))

        assert [e.type for e in bus.events] == [
            EventType.AcquisitionState,
            EventType.CommandResult,
        ]
        assert bus.events[0].data.connected is True
        assert bus.events[0].data.streaming is False
        assert bus.events[-1].data.ok is True

    def test_confirmed_internal_failure_is_not_reported_as_rejection(self):
        class _BrokenSource(_FakeDataSource):
            async def connect(self):
                raise OSError("device unavailable")

        bus = _SpyBus()
        a = AcquisitionRuntime(
            data_source=_BrokenSource(),
            command_queue=_FakeQueue(),
            data_manager=_FakeDataManager(),
            event_bus=bus,
            source_id="test-source",
            preparation_required=False,
        )

        _run(a.execute((
            AcquisitionCommand.connect,
            CommandRequest(command_id="cmd-failed"),
        )))

        result = next(e.data for e in bus.events if e.type == EventType.CommandResult)
        assert result.ok is False
        assert result.error_code == "OSError"
        assert result.failure_kind is CommandFailureKind.EXECUTION_FAILED

    def test_stop_stream_stops_source_and_cancels_fetch_task(self):
        async def scenario():
            a = _acq(data_source=_FakeDataSource(fail_after=0))
            await a.execute(AcquisitionCommand.connect)
            await a.execute(AcquisitionCommand.start_stream)
            task = a.fetch_task
            await a.execute(AcquisitionCommand.stop_stream)
            await asyncio.sleep(0)  # let the cancellation propagate one loop turn
            assert "stop_stream" in a.data_source.calls
            assert a.data_source.is_connected is True
            assert a.data_source.is_streaming is False
            assert task.cancelled() or task.done()
            assert a.fetch_task is None
        _run(scenario())

    def test_stop_stream_joins_orphan_fetch_task_when_source_already_stopped(self):
        async def scenario():
            a = _acq()
            a.fetch_task = asyncio.create_task(asyncio.Event().wait())
            task = a.fetch_task

            await a.execute(AcquisitionCommand.stop_stream)

            assert task.done()
            assert a.fetch_task is None

        _run(scenario())

    def test_stop_acquisition_sets_should_exit(self):
        a = _acq()
        assert a.should_exit is False
        _run(a.execute(AcquisitionCommand.stop_acquisition))
        assert a.should_exit is True

    def test_get_metadata_queries_source(self):
        a = _acq()
        _run(a.execute(AcquisitionCommand.connect))
        _run(a.execute(AcquisitionCommand.get_metadata))
        assert a.data_source.calls == ["connect", "get_metadata"]


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
# Scheme A: state projection — the worker emits an AcquisitionSnapshot on
# every state transition.
# ---------------------------------------------------------------------------

class _SpyBus:
    def __init__(self):
        self.events = []

    def put_event(self, event) -> None:
        self.events.append(event)


class _EOFDataSource(_FakeDataSource):
    """Plays out its chunks, then signals a clean end of stream (None)."""

    async def read_chunk(self):
        if self._read_idx < len(self._chunks):
            chunk = self._chunks[self._read_idx]
            self._read_idx += 1
            return chunk
        return None


class TestStateProjectionEmission:
    def _make(self, source):
        from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime
        bus = _SpyBus()
        acq = AcquisitionRuntime(
            data_source=source,
            command_queue=_FakeQueue(),
            data_manager=_FakeDataManager(),
            event_bus=bus,
            source_id="test-source",
            preparation_required=False,
        )
        return acq, bus

    def _snaps(self, bus):
        from omnibci.shared.events import EventType
        return [(e.data.connected, e.data.streaming)
                for e in bus.events if e.type == EventType.AcquisitionState]

    def test_connect_and_stream_emit_snapshots(self):
        source = _FakeDataSource()
        acq, bus = self._make(source)
        _run(acq._on_connect())
        assert self._snaps(bus)[-1] == (True, False)
        _run(acq._on_stop_stream())
        assert self._snaps(bus)[-1] == (True, False)

    def test_snapshots_identify_reconnections_even_when_both_are_connected(self):
        acq, bus = self._make(_FakeDataSource())
        _run(acq._on_connect())
        first = bus.events[-1].data
        assert first.connection_id == acq.connection_id
        assert first.connection_id
        _run(acq._on_connect())
        assert bus.events[-1].data.connection_id == first.connection_id
        _run(acq._on_disconnect())
        assert bus.events[-1].data.connection_id is None
        _run(acq._on_connect())
        second = bus.events[-1].data
        assert first.connected and second.connected
        assert second.connection_id == acq.connection_id
        assert first.connection_id != second.connection_id

    def test_preconnected_source_is_projected_without_reconnecting(self):
        source = _FakeDataSource()
        source._is_connected = True
        acq, bus = self._make(source)
        connection_id = acq.connection_id

        _run(acq._on_connect())

        assert source.calls == []
        assert self._snaps(bus) == [(True, False)]
        assert connection_id
        assert bus.events[-1].data.connection_id == connection_id

    def test_eof_closes_stream_and_emits_truth(self):
        """EOF stops the source and projects streaming=False to clients."""
        source = _EOFDataSource(chunks=[np.ones((2, 3))])
        acq, bus = self._make(source)
        _run(acq._on_connect())

        async def start_and_drain():
            await acq._on_start_stream()
            await acq.fetch_task

        _run(start_and_drain())
        assert "stop_stream" in source.calls          # EOF closes the stream cleanly
        assert source.is_streaming is False
        assert self._snaps(bus)[-1] == (True, False)  # projection reflects source truth

    def test_start_stream_when_not_connected_reports_truth(self):
        source = _FakeDataSource()
        acq, bus = self._make(source)          # never connected
        with pytest.raises(AcquisitionCommandRejected):
            _run(acq._on_start_stream())
        assert "start_stream" not in source.calls
        assert self._snaps(bus)[-1] == (False, False)

    def test_no_bus_is_silent_noop(self):
        from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime
        acq = AcquisitionRuntime(
            data_source=_FakeDataSource(),
            command_queue=_FakeQueue(),
            data_manager=_FakeDataManager(),
            source_id="test-source",
            preparation_required=False,
        )
        _run(acq._on_connect())   # must not raise despite event_bus=None
