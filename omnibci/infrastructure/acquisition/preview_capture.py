"""Finite, best-effort copies of selected normalized acquisition samples."""
import logging
import pickle
from queue import Full
from time import monotonic

from omnibci.application.acquisition_preview import (
    CAPTURE_TIMEOUT_SECONDS, MAX_CACHE_BYTES, MAX_FRAME_BYTES, PreviewSelection, limits,
)


logger = logging.getLogger(__name__)


class PreviewCapture:
    def __init__(self, queue, *, clock=monotonic):
        self.queue = queue
        self._clock = clock
        self._deadline = None
        self._selection = None
        self._state = {
            "capture_id": None, "generation": None, "config_revision": None,
            "source_id": None, "connection_id": None, "status": "idle", "reason": None,
            "observed": 0, "enqueued": 0, "dropped": 0, "oversized": 0, "enqueued_bytes": 0,
        }

    def start(self, capture_id: str, selection: PreviewSelection, identity: dict) -> dict:
        self.expire()
        if self._state["status"] == "capturing":
            raise ValueError("A preview capture is already active")
        if self.queue is None:
            raise ValueError("Acquisition preview is unavailable")
        if not isinstance(capture_id, str) or not capture_id or len(capture_id) > 128:
            raise ValueError("A valid capture_id is required")
        self._selection = selection
        self._deadline = self._clock() + CAPTURE_TIMEOUT_SECONDS
        self._state = {
            "capture_id": capture_id, **identity, "status": "capturing", "reason": None,
            "observed": 0, "enqueued": 0, "dropped": 0, "oversized": 0, "enqueued_bytes": 0,
        }
        return self.snapshot()

    def stop(self, capture_id: str) -> dict:
        if not capture_id or capture_id != self._state["capture_id"]:
            raise ValueError("Preview capture does not match the requested capture_id")
        self.expire()
        self.finish("user_stop")
        return self.snapshot()

    def finish(self, reason: str) -> None:
        if self._state["status"] == "capturing":
            self._state.update(status="finished", reason=reason)
            self._deadline = None

    def expire(self) -> None:
        if self._deadline is not None and self._clock() >= self._deadline:
            self.finish("timeout")

    @property
    def active(self) -> bool:
        self.expire()
        return self._state["status"] == "capturing"

    def snapshot(self) -> dict:
        self.expire()
        return {**self._state, "selection": self._selection.to_dict() if self._selection else None,
                "limits": limits()}

    def offer(self, chunk, identity: dict, *, descriptor=None) -> None:
        self.expire()
        if self._state["status"] != "capturing" or any(
            identity.get(key) != self._state[key]
            for key in ("generation", "config_revision", "connection_id", "source_id")
        ):
            return
        self._state["observed"] += 1
        selection = self._selection
        raw = chunk.raw_data
        if raw.dtype.kind not in "biuf" or raw.dtype.itemsize > 8:
            self._state["dropped"] += 1
            self.finish("unsupported_dtype")
            return
        if any(row >= raw.shape[0] - 1 for row in selection.rows):
            self._state["dropped"] += 1
            self.finish("invalid_selection")
            return
        rows = list(selection.rows) + ([raw.shape[0] - 1] if selection.include_trigger else [])
        start = selection.sample_start
        stop = min(start + selection.sample_count, raw.shape[1])
        samples = max(0, stop - start)
        try:
            if len(rows) * samples * raw.dtype.itemsize > MAX_FRAME_BYTES:
                self._state["oversized"] += 1
                self._state["dropped"] += 1
                return
            frame = {
                "capture_id": self._state["capture_id"], **identity,
                "sequence_id": chunk.sequence_id, "timestamp_ns": str(chunk.timestamp),
                "original_shape": list(raw.shape), "dtype": str(raw.dtype),
                "row_indices": rows, "sample_start": start, "sample_count": samples,
                "trigger_row_index": raw.shape[0] - 1,
                "values": raw[rows, min(start, raw.shape[1]):stop].copy(), "descriptor": descriptor,
            }
            encoded = pickle.dumps(frame, protocol=pickle.HIGHEST_PROTOCOL)
            if len(encoded) > MAX_FRAME_BYTES:
                self._state["oversized"] += 1
                self._state["dropped"] += 1
                return
            if self._state["enqueued_bytes"] + len(encoded) > MAX_CACHE_BYTES:
                self._state["dropped"] += 1
                self.finish("byte_limit")
                return
            self.queue.put_nowait(encoded)
            self._state["enqueued"] += 1
            self._state["enqueued_bytes"] += len(encoded)
        except Full:
            self._state["dropped"] += 1
        except Exception:
            self._state["dropped"] += 1
            self.finish("preview_error")
            logger.exception("Acquisition preview failed; the signal stream is unaffected")
        finally:
            if self._state["observed"] >= selection.chunk_count:
                self.finish("chunk_limit")
