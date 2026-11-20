"""Exercise the source contract through the acquisition owner, without plugin flags."""
import asyncio
from unittest.mock import MagicMock
from threading import Event

import numpy as np
import pytest

from omnibci.domain.ports import acquisition as port
from omnibci.domain.signal_info import ChannelDescriptor, SignalInfo
from omnibci.infrastructure.acquisition.acquisition import AcquisitionRuntime, AcquisitionCommandRejected
from omnibci.application.events import SignalPreparationReceipt, CommandRequest
from omnibci.application.source_metadata import source_fingerprint


class Source:
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


def runtime(source):
    return AcquisitionRuntime(source, MagicMock(), MagicMock(), source_id="source")


@pytest.mark.asyncio
async def test_owner_connects_and_stops_reader_before_driver_without_public_flags():
    source = Source()
    owner = runtime(source)
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
    source = Source()
    source.ignore_cancel = True
    owner = runtime(source)
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
    source = Source()
    setattr(source, field, value)
    with pytest.raises(ValueError, match=field):
        runtime(source)
    assert source.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [np.zeros((2, 0)), np.zeros((3, 1)),
                                  np.array([[1.0], [-1.0]]), np.array([[1.0], [0.5]]),
                                  np.array([[1.0], [np.nan]]), np.array([[1.0], [np.inf]])])
async def test_invalid_source_matrix_is_rejected_before_delivery(data):
    source = Source()
    async def observe():
        return port.SourceObservation(native_text="native", signal_info=SignalInfo(
            sample_rate=250, channels=(ChannelDescriptor(channel_id="c3", index=0, name="C3"),)),
            native_event="final-row", provenance="device-reported")
    async def read():
        return data
    source.observe = observe
    source.read_chunk = read
    owner = runtime(source)
    await owner._on_connect()
    await owner._on_get_metadata()
    await owner._on_start_stream()
    await owner.fetch_task
    assert owner._fetch_failure
    owner.data_manager.put_data.assert_not_called()
    await owner._on_disconnect()


@pytest.mark.asyncio
async def test_failed_partial_connection_cleanup_blocks_replacement_until_disconnected():
    source = Source()
    async def connect():
        source.calls.append("connect")
        raise ValueError("incomplete handshake")
    async def disconnect():
        raise RuntimeError("resource still busy")
    source.connect, source.disconnect = connect, disconnect
    owner = runtime(source)
    with pytest.raises(ValueError, match="handshake"):
        await owner._on_connect()
    with pytest.raises(AcquisitionCommandRejected, match="cleanup"):
        await owner._on_connect()
    assert source.calls == ["connect"]
    source.disconnect = Source.disconnect.__get__(source)
    await owner._on_disconnect()
    source.connect = Source.connect.__get__(source)
    await owner._on_connect()
    assert owner.connection_id
    await owner._on_disconnect()


@pytest.mark.asyncio
async def test_failed_disconnect_cannot_restart_stream_using_an_invalidated_connection():
    source = Source()
    async def disconnect():
        raise RuntimeError("resource still busy")
    owner = runtime(source)
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
    source = Source()
    info = SignalInfo(sample_rate=250, channels=(ChannelDescriptor(channel_id="c3", index=0, name="C3"),))
    async def observe():
        return port.SourceObservation(native_text="native", signal_info=info,
                                      native_event="final-row", provenance="device-reported")
    source.observe = observe
    owner = runtime(source)
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
    source = Source()
    stopping, release = asyncio.Event(), asyncio.Event()
    reads = 0
    async def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return None
        return await Source.read_chunk(source)
    async def stop():
        stopping.set()
        await release.wait()
        if stop_fails:
            raise RuntimeError("EOF stop failed")
    source.read_chunk, source.stop_stream = read, stop
    owner = runtime(source)
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
        source.stop_stream = Source.stop_stream.__get__(source)
        await owner._on_disconnect()


@pytest.mark.asyncio
async def test_observation_after_clean_eof_needs_no_extra_stop_command():
    source = Source()
    async def read():
        return None
    async def observe():
        return port.SourceObservation(native_text="native", signal_info=None, native_event="final-row",
                                      provenance="protocol-defined", layout_error="No layout")
    source.read_chunk, source.observe = read, observe
    owner = runtime(source)
    await owner._on_connect()
    await owner._on_start_stream()
    await owner.fetch_task
    assert (await owner._on_get_metadata())["metadata"] == "native"
    await owner._on_disconnect()
