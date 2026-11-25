"""Transport loss invalidates the source identity without erasing continuity loss."""
import asyncio
import errno
import queue

import pytest

from omnibci.application.events import CommandFailureKind, CommandRequest
from omnibci.infrastructure.acquisition.acquisition import (
    AcquisitionCommand,
    AcquisitionRuntime,
)
from omnibci.shared.events import EventType


class Source:
    trigger_mode = "events"
    resume_mode = "restart-only"
    def __init__(self):
        self.is_connected = True
        self.is_streaming = False
        self.calls = []

    async def connect(self):
        self.calls.append("connect")
        self.is_connected = True

    async def disconnect(self):
        self.calls.append("disconnect")
        self.is_connected = False
        self.is_streaming = False

    async def start_stream(self):
        self.calls.append("start_stream")
        self.is_streaming = True

    async def stop_stream(self):
        self.calls.append("stop_stream")
        self.is_streaming = False


class Bus:
    def __init__(self):
        self.events = []

    def put_event(self, event):
        self.events.append(event)


class Sink:
    def __init__(self):
        self.items = []

    def put_data(self, item):
        self.items.append(item)


def runtime(source=None, *, projected=True):
    acquisition = AcquisitionRuntime(
        source or Source(), queue.Queue(), Sink(), Bus() if projected else None,
        source_id="test-source",
        generation="generation", acquisition_revision="revision", engine_revision="engine",
    )
    acquisition._connected = True
    acquisition._streaming = (source or acquisition.data_source).is_streaming
    acquisition.connection_id = "connection"
    acquisition._observation = object()
    return acquisition


def assert_disconnected(acquisition):
    assert acquisition.connection_id is None
    assert acquisition._observation is None
    if acquisition.event_bus:
        states = [e.data for e in acquisition.event_bus.events if e.type is EventType.AcquisitionState]
        assert states
        assert not states[-1].connected
        assert not states[-1].streaming
        assert states[-1].connection_id is None


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("error", [asyncio.IncompleteReadError(b"partial", 20),
                                 OSError(errno.EBADF, "closed socket"), OSError(errno.ENOTCONN, "not connected")])
def test_fetch_transport_loss_invalidates_identity_and_metadata(projected, error):
    async def scenario():
        source = Source()
        source.is_streaming = True

        async def read_chunk():
            source.is_connected = source.is_streaming = False
            raise error

        source.read_chunk = read_chunk
        acquisition = runtime(source, projected=projected)
        await acquisition.fetch()

        assert_disconnected(acquisition)
        assert type(error).__name__ in acquisition._fetch_failure

    asyncio.run(scenario())


@pytest.mark.parametrize("command", [
    AcquisitionCommand.get_metadata,
    AcquisitionCommand.start_stream,
])
@pytest.mark.parametrize("correlated", [False, True])
def test_command_transport_failure_publishes_disconnection_before_failure(command, correlated):
    async def scenario():
        source = Source()
        source.is_streaming = False

        async def lose_connection():
            source.is_connected = source.is_streaming = False
            raise ConnectionResetError("peer closed during command")

        setattr(source, "observe" if command is AcquisitionCommand.get_metadata else command.name, lose_connection)
        acquisition = runtime(source)
        # Force the metadata command to use the source, including without a request.
        acquisition._observation = None
        data = {"refresh": True} if command is AcquisitionCommand.get_metadata else None
        payload = CommandRequest("failed-command", data) if correlated else None
        item = (command, payload)
        if correlated:
            await acquisition.execute(item)
            state_event, result_event = acquisition.event_bus.events[-2:]
            assert state_event.type is EventType.AcquisitionState
            assert result_event.type is EventType.CommandResult
            assert result_event.data.error_code == "ConnectionResetError"
            assert result_event.data.failure_kind is CommandFailureKind.EXECUTION_FAILED
        else:
            with pytest.raises(ConnectionResetError, match="peer closed"):
                await acquisition.execute(item)
        assert_disconnected(acquisition)

    asyncio.run(scenario())


def test_metadata_timeout_keeps_connection_and_is_not_continuity_loss():
    async def scenario():
        source = Source()

        async def timeout():
            raise TimeoutError("metadata reply delayed")

        source.observe = timeout
        acquisition = runtime(source)
        connection_id = acquisition.connection_id
        await acquisition.execute((AcquisitionCommand.get_metadata, CommandRequest("metadata", {"refresh": True})))

        state, result = acquisition.event_bus.events[-2:]
        assert state.data.connected
        assert state.data.connection_id == connection_id
        assert result.data.error_code == "SOURCE_METADATA_TIMEOUT"
        assert result.data.failure_kind is CommandFailureKind.REJECTED
        assert acquisition._fetch_failure is None

    asyncio.run(scenario())


class DelayedTeardownSource(Source):
    trigger_mode = 'events'
    resume_mode = "restart-only"
    def __init__(self):
        super().__init__()
        self.is_streaming = True
        self.teardown_started = asyncio.Event()
        self.release_teardown = asyncio.Event()

    async def read_chunk(self):
        self.is_connected = self.is_streaming = False
        raise ConnectionResetError("original peer failure")

    async def stop_stream(self):
        self.calls.append("stop_stream_started")
        self.teardown_started.set()
        await self.release_teardown.wait()
        self.is_streaming = False
        self.calls.append("stop_stream_finished")


async def start_failing_reader():
    source = DelayedTeardownSource()
    acquisition = runtime(source)
    acquisition.fetch_task = asyncio.create_task(acquisition.fetch())
    await asyncio.wait_for(source.teardown_started.wait(), timeout=1)
    return acquisition, source, acquisition.fetch_task


def test_connect_waits_for_old_teardown_without_losing_failure_or_stopping_new_connection():
    async def scenario():
        acquisition, source, old_task = await start_failing_reader()
        previous_id = acquisition.connection_id
        connecting = asyncio.create_task(acquisition.execute((AcquisitionCommand.connect, CommandRequest("connect"))))
        await asyncio.sleep(0)
        assert not connecting.done()
        assert source.calls == ["stop_stream_started"]

        source.release_teardown.set()
        await asyncio.wait_for(connecting, timeout=1)

        assert old_task.done() and not old_task.cancelled()
        assert acquisition.fetch_task is None
        assert source.calls == ["stop_stream_started", "stop_stream_finished", "disconnect", "connect"]
        assert source.is_connected
        assert acquisition.connection_id and acquisition.connection_id != previous_id
        assert acquisition._fetch_failure == "ConnectionResetError: original peer failure"
        assert acquisition.event_bus.events[-1].data.ok
        # A new socket cannot authorize resuming a stream whose data was lost.
        await acquisition.execute((AcquisitionCommand.start_stream, CommandRequest("read")))
        result = acquisition.event_bus.events[-1].data
        assert result.error_code == "ACQUISITION_STREAM_CONTINUITY_LOST"
        assert result.failure_kind is CommandFailureKind.REJECTED
        assert "Stop engine" in result.message and "Start engine" in result.message
        assert "Connect" in result.message and "Start reading" in result.message
        assert "start_stream" not in source.calls

    asyncio.run(scenario())


def test_connect_timeout_does_not_cancel_old_teardown_or_create_new_connection():
    async def scenario():
        acquisition, source, old_task = await start_failing_reader()
        acquisition.FETCH_FINISH_TIMEOUT_S = 0.01
        await acquisition.execute((AcquisitionCommand.connect, CommandRequest("connect")))

        result = acquisition.event_bus.events[-1].data
        assert result.error_code == "ACQUISITION_STOP_PENDING"
        assert result.failure_kind is CommandFailureKind.REJECTED
        assert acquisition.fetch_task is old_task
        assert not old_task.done() and not old_task.cancelling()
        assert source.calls == ["stop_stream_started"]
        assert_disconnected(acquisition)

        source.release_teardown.set()
        await asyncio.wait_for(old_task, timeout=1)
        await acquisition.execute((AcquisitionCommand.connect, CommandRequest("retry")))
        assert acquisition.event_bus.events[-1].data.ok
        assert source.calls[-1] == "connect"
        assert acquisition._fetch_failure == "ConnectionResetError: original peer failure"

    asyncio.run(scenario())


def test_cancelled_connect_does_not_cancel_old_teardown():
    async def scenario():
        acquisition, source, old_task = await start_failing_reader()
        connecting = asyncio.create_task(acquisition.execute((AcquisitionCommand.connect, CommandRequest("connect"))))
        await asyncio.sleep(0)
        connecting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connecting

        assert acquisition.fetch_task is old_task
        assert not old_task.done() and not old_task.cancelling()
        assert "connect" not in source.calls
        assert_disconnected(acquisition)
        source.release_teardown.set()
        await asyncio.wait_for(old_task, timeout=1)
        assert acquisition._fetch_failure == "ConnectionResetError: original peer failure"

    asyncio.run(scenario())


def test_stop_write_failure_latches_loss_after_joining_pending_reader():
    async def scenario():
        source = Source()
        source.is_streaming = True
        reading = asyncio.Event()

        async def read_chunk():
            reading.set()
            await asyncio.Event().wait()

        async def broken_stop():
            source.is_connected = source.is_streaming = False
            raise BrokenPipeError("stop write failed")

        source.read_chunk = read_chunk
        source.stop_stream = broken_stop
        acquisition = runtime(source)
        acquisition.fetch_task = asyncio.create_task(acquisition.fetch())
        old_task = acquisition.fetch_task
        await asyncio.wait_for(reading.wait(), timeout=1)
        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("stop")))

        assert old_task.done()
        assert acquisition.fetch_task is None
        assert acquisition._fetch_failure == "BrokenPipeError: stop write failed"
        assert_disconnected(acquisition)
        assert acquisition.event_bus.events[-1].data.error_code == "BrokenPipeError"
        await acquisition.execute((AcquisitionCommand.disconnect, None))
        await acquisition.execute((AcquisitionCommand.connect, None))
        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("drain")))
        result = acquisition.event_bus.events[-1].data
        assert result.error_code == "ACQUISITION_STREAM_CONTINUITY_LOST"
        assert result.failure_kind is CommandFailureKind.REJECTED
        assert acquisition.data_manager.items == []

    asyncio.run(scenario())


class _CheckpointSource:
    trigger_mode = "events"
    resume_mode = "checkpoint"
    is_streaming = True
    is_connected = True

    def __init__(self, *, malformed_chunk=False):
        self.cursor = 0
        self.malformed_chunk = malformed_chunk

    async def read_chunk(self):
        import numpy as np

        self.cursor += 4
        return np.ones(4) if self.malformed_chunk else np.ones((2, 4))

    async def stop_stream(self):
        self.is_streaming = False

    async def disconnect(self):
        self.is_connected = False

    async def connect(self):
        self.is_connected = True

    async def start_stream(self):
        self.cursor = 0
        self.is_streaming = True

    def checkpoint(self):
        from omnibci.domain.ports.acquisition import SourceCheckpoint

        return SourceCheckpoint(resource_id="source", resource_revision="rev", state={"cursor": self.cursor})


@pytest.mark.parametrize("failure", ["malformed_chunk", "read"])
def test_terminal_fetch_failure_blocks_drain_checkpoint_and_manual_restart(failure):
    from omnibci.application.events import CommandResult
    from omnibci.infrastructure.ipc.data_manager import DataManager

    async def scenario():
        source, bus = _CheckpointSource(malformed_chunk=failure == "malformed_chunk"), Bus()
        data = DataManager(queue.Queue(maxsize=1))
        acquisition = AcquisitionRuntime(
            source, queue.Queue(), data, bus,
            source_id="test-source",
            generation="generation", acquisition_revision="revision", engine_revision="engine",
        )
        await acquisition._on_connect()
        acquisition._streaming = True
        if failure == "read":
            async def fail_read():
                raise OSError("device read failed")
            source.read_chunk = fail_read
        await acquisition.fetch()
        assert not source.is_streaming
        assert acquisition._last_sequence == -1
        if failure != "read":
            assert source.cursor == 4
        while data.get_data(0) is not None:
            pass
        for command, payload in (
            (AcquisitionCommand.stop_and_drain, None),
            (AcquisitionCommand.get_checkpoint, {"generation": "generation", "expected_revision": "revision", "engine_revision": "engine"}),
            (AcquisitionCommand.start_stream, None),
        ):
            await acquisition.execute((command, CommandRequest(command.name, payload)))
            result = bus.events[-1].data
            assert isinstance(result, CommandResult)
            assert not result.ok
            assert "lost stream continuity" in result.message
            if command is AcquisitionCommand.get_checkpoint:
                assert result.error_code == "ACQUISITION_CONFIGURATION_FAILED"
                assert result.failure_kind is CommandFailureKind.EXECUTION_FAILED
            else:
                assert result.error_code == "ACQUISITION_STREAM_CONTINUITY_LOST"
                assert result.failure_kind is CommandFailureKind.REJECTED
                assert "Stop engine" in result.message and "Start engine" in result.message
            assert data.get_data(0) is None
        await acquisition.execute((AcquisitionCommand.disconnect, None))
        await acquisition.execute((AcquisitionCommand.connect, None))
        await acquisition.execute((AcquisitionCommand.stop_and_drain, CommandRequest("retry")))
        assert not bus.events[-1].data.ok
        assert data.get_data(0) is None

    asyncio.run(scenario())


def test_fresh_graph_has_no_stale_failure_latch():
    from omnibci.infrastructure.ipc.data_manager import DataManager

    async def scenario():
        acquisition = AcquisitionRuntime(
            _CheckpointSource(), queue.Queue(), DataManager(queue.Queue()), Bus(),
            source_id="test-source",
        )
        await acquisition._on_stop_stream()
        acquisition._require_healthy_stream()
        assert acquisition._fetch_failure is None

    asyncio.run(scenario())


class ContractSource:
    trigger_mode = "events"
    resume_mode = "restart-only"

    def __init__(self):
        self.calls = []
        self.reading = asyncio.Event()
        self.release = asyncio.Event()
        self.ignore_cancel = False

    async def connect(self):
        self.calls.append("connect")

    async def start_stream(self):
        self.calls.append("start")

    async def read_chunk(self):
        self.reading.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self.ignore_cancel:
                await self.release.wait()
            raise
        finally:
            self.calls.append("read_settled")

    async def stop_stream(self):
        self.calls.append("stop")

    async def disconnect(self):
        self.calls.append("disconnect")


def _contract_runtime(source):
    from unittest.mock import MagicMock

    return AcquisitionRuntime(source, MagicMock(), MagicMock(), source_id="source")


@pytest.mark.asyncio
async def test_owner_connects_and_stops_reader_before_driver_without_public_flags():
    source = ContractSource()
    owner = _contract_runtime(source)
    await owner._on_connect()
    connection = owner.connection_id
    await owner._on_connect()
    assert connection and owner.connection_id == connection
    await owner._on_start_stream()
    await source.reading.wait()
    await owner._on_disconnect()
    assert source.calls == ["connect", "start", "read_settled", "stop", "disconnect"]
    assert owner.fetch_task is None and owner.connection_id is None


@pytest.mark.asyncio
async def test_unsettled_reader_blocks_stop_disconnect_and_replacement():
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommandRejected

    source = ContractSource()
    source.ignore_cancel = True
    owner = _contract_runtime(source)
    owner.FETCH_FINISH_TIMEOUT_S = 0.01
    await owner._on_connect()
    await owner._on_start_stream()
    await source.reading.wait()
    try:
        with pytest.raises(AcquisitionCommandRejected, match="stopping"):
            await owner._on_stop_stream()
        assert owner.fetch_task is not None and not owner.fetch_task.done()
        with pytest.raises(AcquisitionCommandRejected):
            await owner._on_disconnect()
        assert "stop" not in source.calls and "disconnect" not in source.calls
        assert source.calls.count("connect") == 1
    finally:
        source.release.set()
        await owner.fetch_task
        with pytest.raises(AcquisitionCommandRejected, match="cleanup"):
            await owner._on_start_stream()
        await owner._on_disconnect()


@pytest.mark.parametrize("field, value", [("trigger_mode", None), ("trigger_mode", "unknown"),
                                         ("resume_mode", None), ("resume_mode", "unknown")])
def test_invalid_modes_fail_before_connect(field, value):
    source = ContractSource()
    setattr(source, field, value)
    with pytest.raises(ValueError, match=field):
        _contract_runtime(source)
    assert source.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    "empty", "wrong_rows", "neg_trigger", "float_trigger", "nan_trigger", "inf_trigger",
])
async def test_invalid_source_matrix_is_rejected_before_delivery(data):
    import numpy as np
    from omnibci.domain.ports import acquisition as port
    from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo

    matrices = {
        "empty": np.zeros((2, 0)),
        "wrong_rows": np.zeros((3, 1)),
        "neg_trigger": np.array([[1.0], [-1.0]]),
        "float_trigger": np.array([[1.0], [0.5]]),
        "nan_trigger": np.array([[1.0], [np.nan]]),
        "inf_trigger": np.array([[1.0], [np.inf]]),
    }
    matrix = matrices[data]
    source = ContractSource()

    async def observe():
        return port.SourceObservation(native_text="native", signal_info=SignalInfo(
            sample_rate=250, channels=(ChannelDescriptor(channel_id="c3", index=0, name="C3"),)),
            native_event="final-row", provenance="device-reported")

    async def read():
        return matrix

    source.observe = observe
    source.read_chunk = read
    owner = _contract_runtime(source)
    await owner._on_connect()
    await owner._on_get_metadata()
    await owner._on_start_stream()
    await owner.fetch_task
    assert owner._fetch_failure
    owner.data_manager.put_data.assert_not_called()
    await owner._on_disconnect()


@pytest.mark.asyncio
async def test_failed_partial_connection_cleanup_blocks_replacement_until_disconnected():
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommandRejected

    source = ContractSource()

    async def connect():
        source.calls.append("connect")
        raise ValueError("incomplete handshake")

    async def disconnect():
        raise RuntimeError("resource still busy")

    source.connect, source.disconnect = connect, disconnect
    owner = _contract_runtime(source)
    with pytest.raises(ValueError, match="handshake"):
        await owner._on_connect()
    with pytest.raises(AcquisitionCommandRejected, match="cleanup"):
        await owner._on_connect()
    assert source.calls == ["connect"]
    source.disconnect = ContractSource.disconnect.__get__(source)
    await owner._on_disconnect()
    source.connect = ContractSource.connect.__get__(source)
    await owner._on_connect()
    assert owner.connection_id
    await owner._on_disconnect()


@pytest.mark.asyncio
async def test_failed_disconnect_cannot_restart_stream_using_an_invalidated_connection():
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommandRejected

    source = ContractSource()

    async def disconnect():
        raise RuntimeError("resource still busy")

    owner = _contract_runtime(source)
    await owner._on_connect()
    source.disconnect = disconnect
    with pytest.raises(RuntimeError, match="busy"):
        await owner._on_disconnect()
    with pytest.raises(AcquisitionCommandRejected, match="cleanup"):
        await owner._on_start_stream()
    assert "start" not in source.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["failed", "layout", "unchanged"])
async def test_observation_refresh_invalidates_only_an_obsolete_processing_attachment(change):
    from threading import Event
    from omnibci.application.events import SignalPreparationReceipt
    from omnibci.application.source_metadata import source_fingerprint
    from omnibci.domain.ports import acquisition as port
    from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo

    source = ContractSource()
    info = SignalInfo(sample_rate=250, channels=(ChannelDescriptor(channel_id="c3", index=0, name="C3"),))

    async def observe():
        return port.SourceObservation(native_text="native", signal_info=info,
                                      native_event="final-row", provenance="device-reported")

    source.observe = observe
    owner = _contract_runtime(source)
    await owner._on_connect()
    before = await owner._on_get_metadata()
    owner.processing_active = Event()
    owner.processing_active.set()
    owner._processing_receipt = SignalPreparationReceipt(
        "generation", "acquisition", "processing", "engine", owner.connection_id,
        source_fingerprint(before["descriptor"]), "a" * 64, "input",
    )

    async def failed_observation():
        raise ValueError("observation failed")

    if change == "failed":
        source.observe = failed_observation
        with pytest.raises(ValueError, match="observation failed"):
            await owner._on_get_metadata(CommandRequest("refresh", {"refresh": True}))
    else:
        if change == "layout":
            info = SignalInfo(sample_rate=500, channels=info.channels)
        await owner._on_get_metadata(CommandRequest("refresh", {"refresh": True}))
    assert owner.processing_active.is_set() is (change == "unchanged")
    assert (owner._processing_receipt is not None) is (change == "unchanged")
    assert owner.connection_id and owner._fetch_failure is None
    await owner._on_disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_fails", [False, True])
async def test_start_waits_for_eof_teardown_and_rechecks_its_result(stop_fails):
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommandRejected

    source = ContractSource()
    stopping, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return None
        return await ContractSource.read_chunk(source)

    async def stop():
        stopping.set()
        await release.wait()
        if stop_fails:
            raise RuntimeError("EOF stop failed")

    source.read_chunk, source.stop_stream = read, stop
    owner = _contract_runtime(source)
    await owner._on_connect()
    await owner._on_start_stream()
    old_task = owner.fetch_task
    await stopping.wait()
    restarting = asyncio.create_task(owner._on_start_stream())
    try:
        await asyncio.sleep(0)
        assert not restarting.done()
        release.set()
        if stop_fails:
            with pytest.raises(AcquisitionCommandRejected, match="continuity"):
                await restarting
            assert source.calls.count("start") == 1
        else:
            await restarting
            await source.reading.wait()
            assert old_task.done() and source.calls.count("start") == 2
    finally:
        release.set()
        await asyncio.gather(old_task, restarting, return_exceptions=True)
        source.stop_stream = ContractSource.stop_stream.__get__(source)
        await owner._on_disconnect()


@pytest.mark.asyncio
async def test_observation_after_clean_eof_needs_no_extra_stop_command():
    from omnibci.domain.ports import acquisition as port

    source = ContractSource()

    async def read():
        return None

    async def observe():
        return port.SourceObservation(native_text="native", signal_info=None, native_event="final-row",
                                      provenance="protocol-defined", layout_error="No layout")

    source.read_chunk, source.observe = read, observe
    owner = _contract_runtime(source)
    await owner._on_connect()
    await owner._on_start_stream()
    await owner.fetch_task
    assert (await owner._on_get_metadata())["metadata"] == "native"
    await owner._on_disconnect()
