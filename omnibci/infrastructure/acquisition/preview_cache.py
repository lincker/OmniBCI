"""One engine graph's bounded, independently consumed diagnostic snapshots."""
import asyncio
from collections import OrderedDict
from copy import deepcopy
import logging
import math
import pickle
from queue import Empty
from threading import Lock

from omnibci.application.acquisition_preview import MAX_CACHE_BYTES, MAX_CHUNKS, MAX_FRAME_BYTES

logger = logging.getLogger(__name__)


def _json_number(value):
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


class PreviewCache:
    def __init__(self, queue, *, generation: str | None) -> None:
        self._queue = queue
        self._generation = generation
        self._frames = OrderedDict()
        self._bytes = 0
        self._lock = Lock()
        self._task = None
        self._stopping = False
        self._available = False
        self._errors = 0

    @property
    def stopped(self) -> bool:
        return self._task is None or self._task.done()

    async def start(self) -> None:
        if self._task is None:
            self._available = True
            self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            await self._task
        self._available = False

    async def _consume(self) -> None:
        while not self._stopping:
            try:
                payload = await asyncio.to_thread(self._queue.get, True, 0.05)
            except Empty:
                continue
            except Exception:
                self._available = False
                logger.exception("Acquisition preview channel unavailable")
                return
            try:
                self.accept(payload)
            except Exception:
                with self._lock:
                    self._errors += 1
                logger.warning("Discarded invalid acquisition preview", exc_info=True)

    def accept(self, payload: bytes) -> None:
        if type(payload) is not bytes or len(payload) > MAX_FRAME_BYTES:
            raise ValueError("Invalid preview frame size")
        # Only the trusted acquisition child writes to this private IPC queue.
        frame = pickle.loads(payload)
        if frame["generation"] != self._generation:
            return
        key = (frame["capture_id"], frame["sequence_id"])
        summary = {name: frame[name] for name in (
            "sequence_id", "timestamp_ns", "original_shape", "dtype",
            "row_indices", "sample_start", "sample_count", "trigger_row_index",
        )}
        summary["encoded_bytes"] = len(payload)
        with self._lock:
            previous = self._frames.pop(key, None)
            if previous is not None:
                self._bytes -= len(previous[0])
            self._frames[key] = (payload, summary)
            self._bytes += len(payload)
            while len(self._frames) > MAX_CHUNKS or self._bytes > MAX_CACHE_BYTES:
                _, (removed, _) = self._frames.popitem(last=False)
                self._bytes -= len(removed)

    def read(self, capture_id: str | None, sequence_id: int | None = None) -> dict:
        with self._lock:
            entries = [(key, value) for key, value in self._frames.items() if key[0] == capture_id]
            selected = self._frames.get((capture_id, sequence_id)) if sequence_id is not None else None
            result = {
                "available": self._available, "cache_errors": self._errors,
                "received": len(entries), "retained_bytes": sum(len(value[0]) for _, value in entries),
                "chunks": deepcopy([value[1] for _, value in entries]),
            }
        if sequence_id is not None:
            # Decode and format outside the lock; each reader owns its result.
            frame = pickle.loads(selected[0]) if selected is not None else None
            if frame is not None:
                frame["values"] = [[_json_number(value) for value in row] for row in frame["values"].tolist()]
            result["chunk"] = frame
        return result
