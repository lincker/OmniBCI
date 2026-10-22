"""Preview transport keeps memory and waiting bounded without a feeder thread."""
from queue import Empty, Full
from time import monotonic

import pytest

from omnibci.application.acquisition_preview import MAX_FRAME_BYTES, PREVIEW_QUEUE_SIZE
from omnibci.infrastructure.acquisition.preview_channel import PreviewChannel


@pytest.fixture
def channel():
    output = PreviewChannel()
    yield output
    output.close()


def test_fixed_capacity_and_fifo_survive_slot_reuse(channel):
    for sequence in range(PREVIEW_QUEUE_SIZE):
        channel.put_nowait(bytes([sequence]) * MAX_FRAME_BYTES)
    with pytest.raises(Full):
        channel.put_nowait(b"overflow")
    first = channel.get(False)
    channel.put_nowait(b"reused")
    for sequence in range(1, PREVIEW_QUEUE_SIZE):
        assert channel.get(False) == bytes([sequence]) * MAX_FRAME_BYTES
    assert channel.get(False) == b"reused"
    assert first == bytes(MAX_FRAME_BYTES)
    with pytest.raises(Empty):
        channel.get(False)


def test_contended_lock_never_blocks_writer_or_reader(channel):
    channel.put_nowait(b"queued")
    channel._lock.acquire()
    try:
        started = monotonic()
        with pytest.raises(Full):
            channel.put_nowait(b"dropped")
        with pytest.raises(Empty):
            channel.get(False)
        with pytest.raises(Empty):
            channel.get(timeout=0.01)
        assert monotonic() - started < 0.5
    finally:
        channel._lock.release()
    assert channel.get(False) == b"queued"


@pytest.mark.parametrize("timeout", [None, 0, 0.01])
def test_empty_reads_have_a_finite_deadline(channel, timeout):
    started = monotonic()
    with pytest.raises(Empty):
        channel.get(timeout=timeout)
    elapsed = monotonic() - started
    assert (0.05 if timeout is None else timeout) <= elapsed < 0.5


def test_empty_bytes_are_a_frame_and_do_not_mean_empty_channel(channel):
    channel.put_nowait(b"")
    assert channel.get(False) == b""
    with pytest.raises(Empty):
        channel.get(False)


def test_expired_poll_does_not_read_a_frame_that_arrives_after_deadline(channel, monkeypatch):
    now = [1.0]

    def resume_after_deadline(delay):
        assert 0 < delay <= 0.005
        now[0] = 2.0
        channel.put_nowait(b"late")

    monkeypatch.setattr("omnibci.infrastructure.acquisition.preview_channel.monotonic", lambda: now[0])
    monkeypatch.setattr("omnibci.infrastructure.acquisition.preview_channel.sleep", resume_after_deadline)
    with pytest.raises(Empty):
        channel.get(timeout=0.05)
    assert channel.get(False) == b"late"


@pytest.mark.parametrize("payload", [None, "text", bytearray(b"mutable"), memoryview(b"view")])
def test_only_encoded_bytes_are_accepted(channel, payload):
    with pytest.raises(TypeError):
        channel.put_nowait(payload)


def test_oversize_payload_does_not_consume_a_slot(channel):
    with pytest.raises(ValueError, match="frame"):
        channel.put_nowait(b"x" * (MAX_FRAME_BYTES + 1))
    with pytest.raises(Empty):
        channel.get(False)


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_unbounded_or_negative_timeouts_are_rejected(channel, timeout):
    with pytest.raises(ValueError, match="timeout"):
        channel.get(timeout=timeout)


def test_close_is_local_idempotent_and_does_not_wait_for_the_lock(channel):
    channel._lock.acquire()
    try:
        channel.close()
        channel.close()
        with pytest.raises(ValueError, match="closed"):
            channel.put_nowait(b"closed")
        with pytest.raises(ValueError, match="closed"):
            channel.get()
    finally:
        channel._lock.release()
