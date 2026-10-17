"""Append-only local raw signal recordings, including the final trigger row.

``chunks.npy`` contains consecutive NumPy arrays; ``chunks.jsonl`` indexes their
byte offsets. Chunk timestamps are producer host acquisition timestamps (Unix
nanoseconds in the built-in acquisition worker), not device sample timestamps.
Interrupted recordings retain their data and a non-completed metadata status.
"""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
from uuid import uuid4

import numpy as np
from platformdirs import user_data_path

from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import SignalInfo


FORMAT = "omnibci.numpy-chunks"
FORMAT_VERSION = 1


class LocalSignalRecorder:
    """NumPy/JSON implementation of the SignalRecorder port."""

    def __init__(self, root: Path | None = None) -> None:
        override = os.environ.get("OMNIBCI_RECORDING_DIR") if root is None else None
        if override is not None and not Path(override).expanduser().is_absolute():
            raise ValueError("OMNIBCI_RECORDING_DIR must be an absolute path")
        self.root = Path(root if root is not None else override if override is not None
                         else user_data_path("OmniBCI", appauthor=False) / "recordings").expanduser().resolve()
        self._metadata = None
        self._info = None
        self._data = None
        self._index = None
        self._closed_sessions = set()
        self._finished = None
        self._failure = None
        self._chunk_count = 0
        self._sample_count = 0
        self._last_sequence = None
        self._rows = None

    def start(self, metadata: dict) -> dict:
        snapshot = deepcopy(metadata)
        session_id = snapshot.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("Recording requires a nonempty session_id")
        if self._metadata is not None and self._finished is None:
            if snapshot == self._metadata and self._failure is None:
                return dict(self._info)
            raise RuntimeError("A recording is already active or failed")
        if session_id in self._closed_sessions:
            raise RuntimeError("A finished recording session cannot be reused")
        descriptor = snapshot.get("signal_info")
        rows = SignalInfo.from_dict(descriptor).output_row_count if descriptor is not None else None
        json.dumps(snapshot, allow_nan=False)
        recording_id = str(uuid4())
        path = self.root / recording_id
        path.mkdir(parents=True)
        self._metadata = snapshot
        self._info = {"id": recording_id, "session_id": session_id, "path": str(path),
                      "format": FORMAT, "format_version": FORMAT_VERSION}
        self._finished = None
        self._failure = None
        self._chunk_count = self._sample_count = 0
        self._last_sequence = None
        self._rows = rows
        try:
            self._write_metadata("recording")
            self._data = (path / "chunks.npy").open("xb")
            self._index = (path / "chunks.jsonl").open("x", encoding="utf-8")
        except Exception as exc:
            self._fail(exc)
            try:
                self.finish(status="failed")
            except Exception:
                pass
            raise
        return dict(self._info)

    def append(self, chunk: SignalChunk, *, config_revision: str | None = None) -> None:
        self._require_active()
        try:
            array = chunk.raw_data
            if not isinstance(array, np.ndarray) or not np.issubdtype(array.dtype, np.number):
                raise ValueError("Recorded signal data must have a numeric NumPy dtype")
            if array.ndim != 2 or array.shape[0] < 2 or (
                self._rows is not None and array.shape[0] != self._rows
            ):
                raise ValueError("Recorded signal rows must match signal_info and include the trigger row")
            if type(chunk.sequence_id) is not int or chunk.sequence_id < 0 or (
                self._last_sequence is not None and chunk.sequence_id <= self._last_sequence
            ):
                raise ValueError("Recorded sequence IDs must be strictly increasing integers")
            if type(chunk.timestamp) not in (int, float) or not math.isfinite(chunk.timestamp):
                raise ValueError("Recorded timestamps must be finite numbers")
            entry = {
                "sequence_id": chunk.sequence_id, "timestamp": chunk.timestamp,
                "offset": self._data.tell(), "samples": array.shape[1],
                "config_revision": config_revision,
            }
            encoded = json.dumps(entry, allow_nan=False) + "\n"
            np.save(self._data, array, allow_pickle=False)
            self._index.write(encoded)
            self._rows = array.shape[0]
            self._last_sequence = chunk.sequence_id
            self._chunk_count += 1
            self._sample_count += array.shape[1]
        except Exception as exc:
            self._fail(exc)
            raise

    def flush(self) -> None:
        self._require_active()
        try:
            for stream in (self._data, self._index):
                stream.flush()
                os.fsync(stream.fileno())
        except Exception as exc:
            self._fail(exc)
            raise

    def finish(self, status: str = "completed", error: str | None = None) -> dict:
        if self._metadata is None:
            raise RuntimeError("No recording has been started")
        if self._finished is not None:
            return dict(self._finished)
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("Recording status must be completed, failed, or interrupted")
        if error is not None:
            self._fail(error)
        failure = None
        # Flush even after an append failed so previously accepted chunks survive.
        for stream in (self._data, self._index):
            if stream is None:
                continue
            try:
                stream.flush()
                os.fsync(stream.fileno())
            except Exception as exc:
                self._fail(exc)
                failure = failure or exc
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    self._fail(exc)
                    failure = failure or exc
        self._data = self._index = None
        final_status = "failed" if self._failure is not None else status
        try:
            self._write_metadata(final_status)
        except Exception as exc:
            self._fail(exc)
            final_status = "failed"
            failure = failure or exc
        self._closed_sessions.add(self._info["session_id"])
        self._finished = {
            **self._info, "status": final_status,
            "chunk_count": self._chunk_count, "sample_count": self._sample_count,
        }
        if self._failure is not None:
            self._finished["error"] = self._failure
        if failure is not None:
            raise failure
        return dict(self._finished)

    def _require_active(self) -> None:
        if self._metadata is None or self._finished is not None:
            raise RuntimeError("No recording is active")
        if self._failure is not None:
            raise RuntimeError(f"Recording has failed: {self._failure}")

    def _fail(self, error) -> None:
        if self._failure is None:
            self._failure = str(error)

    def _write_metadata(self, status: str) -> None:
        document = {
            **self._metadata, **self._info, "status": status,
            "chunk_count": self._chunk_count, "sample_count": self._sample_count,
            "timestamp_basis": "producer host acquisition time; built-in worker uses Unix nanoseconds",
        }
        if self._failure is not None:
            document["error"] = self._failure
        path = Path(self._info["path"])
        temporary = path / "metadata.json.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path / "metadata.json")


def iter_recorded_chunks(path: Path | str):
    """Read indexed raw chunks lazily without loading an entire recording."""
    path = Path(path)
    with (path / "metadata.json").open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    if not isinstance(metadata, dict):
        raise ValueError("Recording metadata must be an object")
    # Initial v1 files predate the explicit format identifier.
    format_id = metadata.get("format", FORMAT)
    version = metadata.get("format_version")
    if format_id != FORMAT or type(version) is not int or version != FORMAT_VERSION:
        raise ValueError(f"Unsupported recording format/version: {format_id!r}/{version!r}")
    with (path / "chunks.npy").open("rb") as data, (path / "chunks.jsonl").open(encoding="utf-8") as index:
        for line in index:
            entry = json.loads(line)
            data.seek(entry["offset"])
            array = np.load(data, allow_pickle=False)
            if array.ndim != 2 or array.shape[1] != entry["samples"]:
                raise ValueError("Recorded array does not match its chunk index")
            yield SignalChunk(timestamp=entry["timestamp"], sequence_id=entry["sequence_id"], raw_data=array)
