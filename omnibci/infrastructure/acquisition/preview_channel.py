"""Bounded preview frames without pipe framing or a queue feeder thread."""
import multiprocessing as mp
from math import isfinite
from queue import Empty, Full
from time import monotonic, sleep

from omnibci.application.acquisition_preview import MAX_FRAME_BYTES, PREVIEW_QUEUE_SIZE


class PreviewChannel:
    """A shared ring whose operations never wait indefinitely for another process.

    A process dying inside the lock makes the channel unavailable until the graph
    is rebuilt. Readers still time out and writers drop frames in that case.
    """

    def __init__(self, context=None):
        context = context or mp.get_context()
        self._buffer = context.RawArray("B", PREVIEW_QUEUE_SIZE * MAX_FRAME_BYTES)
        self._lengths = context.RawArray("i", PREVIEW_QUEUE_SIZE)
        self._read_index = context.RawValue("i", 0)
        self._count = context.RawValue("i", 0)
        self._lock = context.Lock()
        self._closed = False

    def put_nowait(self, payload: bytes) -> None:
        if self._closed:
            raise ValueError("Preview channel is closed")
        if type(payload) is not bytes:
            raise TypeError("Preview frames must be encoded bytes")
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError("Preview frame exceeds the byte limit")
        if not self._lock.acquire(False):
            raise Full
        try:
            if self._count.value == PREVIEW_QUEUE_SIZE:
                raise Full
            index = (self._read_index.value + self._count.value) % PREVIEW_QUEUE_SIZE
            start = index * MAX_FRAME_BYTES
            memoryview(self._buffer).cast("B")[start:start + len(payload)] = payload
            self._lengths[index] = len(payload)
            self._count.value += 1
        finally:
            self._lock.release()

    def get(self, block: bool = True, timeout: float | None = 0.05) -> bytes:
        # None retains a finite default; this diagnostics channel has no unbounded read.
        timeout = 0.05 if timeout is None else timeout
        if not isfinite(timeout) or timeout < 0:
            raise ValueError("Preview timeout must be finite and non-negative")
        deadline = monotonic() + timeout
        while True:
            if self._closed:
                raise ValueError("Preview channel is closed")
            if self._lock.acquire(False):
                try:
                    if self._count.value:
                        index = self._read_index.value
                        start = index * MAX_FRAME_BYTES
                        payload = bytes(memoryview(self._buffer).cast("B")[
                            start:start + self._lengths[index]
                        ])
                        self._read_index.value = (index + 1) % PREVIEW_QUEUE_SIZE
                        self._count.value -= 1
                        return payload
                finally:
                    self._lock.release()
            remaining = deadline - monotonic()
            if not block or remaining <= 0:
                raise Empty
            sleep(min(0.005, remaining))
            if monotonic() >= deadline:
                raise Empty

    def close(self) -> None:
        """Close only this process's handle without waiting for a shared lock."""
        self._closed = True
