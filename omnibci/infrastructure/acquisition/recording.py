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
from omnibci.infrastructure.storage_lock import StorageLease


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
        self._lease = None
        self._checkpoint = _empty_prefix()
        self._data_bytes = self._index_bytes = 0

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
        self._lease = StorageLease(path / ".writer.lock")
        self._metadata = snapshot
        self._info = {"id": recording_id, "session_id": session_id, "path": str(path),
                      "format": FORMAT, "format_version": FORMAT_VERSION}
        self._finished = None
        self._failure = None
        self._chunk_count = self._sample_count = 0
        self._last_sequence = None
        self._rows = rows
        self._checkpoint = _empty_prefix()
        self._data_bytes = self._index_bytes = 0
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
            if config_revision is not None and not isinstance(config_revision, str):
                raise ValueError("Recorded configuration revision must be a string or null")
            entry = {
                "sequence_id": chunk.sequence_id, "timestamp": chunk.timestamp,
                "offset": self._data.tell(), "samples": array.shape[1],
                "config_revision": config_revision,
            }
            encoded = json.dumps(entry, allow_nan=False) + "\n"
            np.save(self._data, array, allow_pickle=False)
            self._index.write(encoded)
            self._data_bytes = self._data.tell()
            self._index_bytes = self._index.tell()
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
            self._write_metadata("recording", checkpoint=self._accepted_prefix())
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
        streams = (self._data, self._index)
        for stream in streams:
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
            checkpoint = self._accepted_prefix() if failure is None else self._checkpoint
            self._write_metadata(final_status, checkpoint=checkpoint)
        except Exception as exc:
            self._fail(exc)
            final_status = "failed"
            failure = failure or exc
            # A directory fsync can fail after replacement. Best effort prevents
            # that visible manifest from advertising a successful finalization.
            try:
                self._write_metadata("failed")
            except Exception:
                pass
        finally:
            if self._lease is not None and all(stream is None or stream.closed for stream in streams):
                self._lease.close()
                self._lease = None
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

    def _accepted_prefix(self) -> dict:
        return {"chunk_count": self._chunk_count, "sample_count": self._sample_count,
                "data_bytes": self._data_bytes, "index_bytes": self._index_bytes}

    def _write_metadata(self, status: str, *, checkpoint: dict | None = None) -> None:
        checkpoint = self._checkpoint if checkpoint is None else checkpoint
        document = {
            **self._metadata, **self._info, "status": status,
            "chunk_count": self._chunk_count, "sample_count": self._sample_count,
            "timestamp_basis": "producer host acquisition time; built-in worker uses Unix nanoseconds",
            "checkpoint": checkpoint,
        }
        if self._failure is not None:
            document["error"] = self._failure
        _write_document(Path(self._info["path"]), document)
        self._checkpoint = dict(checkpoint)


def _read_metadata(path: Path) -> dict:
    with (path / "metadata.json").open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    if not isinstance(metadata, dict):
        raise ValueError("Recording metadata must be an object")
    # Initial v1 files predate the explicit format identifier.
    format_id = metadata.get("format", FORMAT)
    version = metadata.get("format_version")
    if format_id != FORMAT or type(version) is not int or version != FORMAT_VERSION:
        raise ValueError(f"Unsupported recording format/version: {format_id!r}/{version!r}")
    return metadata


def _empty_prefix() -> dict:
    return {"chunk_count": 0, "sample_count": 0, "data_bytes": 0, "index_bytes": 0}


def _write_document(path: Path, document: dict) -> None:
    temporary = path / "metadata.json.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path / "metadata.json")
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _iter_entries(path: Path, metadata: dict, *, limit: int | None = None):
    if limit == 0:
        return
    descriptor = metadata.get("signal_info")
    rows = SignalInfo.from_dict(descriptor).output_row_count if descriptor is not None else None
    previous_sequence = -1
    with (path / "chunks.npy").open("rb") as data, (path / "chunks.jsonl").open("rb") as index:
        number = 0
        while limit is None or number < limit:
            line = index.readline()
            if not line:
                break
            try:
                if not line.endswith(b"\n"):
                    raise ValueError("Incomplete chunk index line")
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise ValueError("Chunk index entry must be an object")
                if type(entry.get("offset")) is not int or entry["offset"] != data.tell():
                    raise ValueError("Chunk offsets must be contiguous")
                if type(entry.get("samples")) is not int or entry["samples"] < 0:
                    raise ValueError("Chunk sample count must be a nonnegative integer")
                if type(entry.get("sequence_id")) is not int or entry["sequence_id"] <= previous_sequence:
                    raise ValueError("Chunk sequence IDs must be strictly increasing integers")
                timestamp = entry.get("timestamp")
                if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
                    raise ValueError("Chunk timestamp must be finite")
                revision = entry.get("config_revision")
                if revision is not None and not isinstance(revision, str):
                    raise ValueError("Chunk configuration revision must be a string or null")
                array = np.load(data, allow_pickle=False)
                if (not isinstance(array, np.ndarray) or not np.issubdtype(array.dtype, np.number)
                        or array.ndim != 2 or array.shape[0] < 2 or array.shape[1] != entry["samples"]
                        or (rows is not None and array.shape[0] != rows)):
                    raise ValueError("Recorded array does not match its chunk index or signal descriptor")
                rows = array.shape[0]
                previous_sequence = entry["sequence_id"]
            except (ValueError, TypeError, KeyError, EOFError, UnicodeError) as exc:
                raise ValueError(f"Invalid recorded chunk {number + 1}: {exc}") from exc
            number += 1
            yield SignalChunk(timestamp, entry["sequence_id"], array), entry, data.tell(), index.tell()


def iter_recorded_chunks(path: Path | str):
    """Read every indexed chunk strictly; malformed tails are never skipped."""
    path = Path(path)
    metadata = _read_metadata(path)
    for chunk, _, _, _ in _iter_entries(path, metadata):
        yield chunk


def inspect_recording(path: Path | str) -> dict:
    """Inspect without mutation; validate the durable prefix before tolerating a tail.

    Complete chunks beyond the checkpoint are readable but were not guaranteed
    durable at the last flush. Legacy files have no durability checkpoint.
    This checks structure and boundaries, not bit-level integrity checksums.
    """
    path = Path(path)
    metadata = _read_metadata(path)
    checkpoint = metadata.get("checkpoint")
    if checkpoint is not None and (
        not isinstance(checkpoint, dict) or set(checkpoint) != set(_empty_prefix())
        or any(type(value) is not int or value < 0 for value in checkpoint.values())
    ):
        raise ValueError("Invalid recording checkpoint")
    prefix = _empty_prefix()
    confirmed = checkpoint is None or checkpoint == prefix
    tail_error = None
    try:
        for chunk, _, data_bytes, index_bytes in _iter_entries(path, metadata):
            prefix = {"chunk_count": prefix["chunk_count"] + 1,
                      "sample_count": prefix["sample_count"] + chunk.raw_data.shape[1],
                      "data_bytes": data_bytes, "index_bytes": index_bytes}
            if checkpoint is not None and prefix["chunk_count"] == checkpoint["chunk_count"]:
                if prefix != checkpoint:
                    raise ValueError("Recording checkpoint does not match its indexed prefix")
                confirmed = True
    except (ValueError, OSError, EOFError) as exc:
        if not confirmed:
            raise ValueError(f"Corruption inside the recording checkpoint: {exc}") from exc
        tail_error = str(exc)
    if not confirmed:
        raise ValueError("Recording ends before its durable checkpoint")
    if metadata.get("status") == "completed" and (
        tail_error is not None or metadata.get("chunk_count") != prefix["chunk_count"]
        or metadata.get("sample_count") != prefix["sample_count"]
        or (checkpoint is not None and checkpoint != prefix)
    ):
        raise ValueError("Completed recording does not match its indexed data")
    sizes = {name: (path / name).stat().st_size if (path / name).exists() else 0
             for name in ("chunks.npy", "chunks.jsonl")}
    return {"id": metadata.get("id"), "session_id": metadata.get("session_id"),
            "path": str(path.resolve()), "format": FORMAT, "format_version": FORMAT_VERSION,
            "status": metadata.get("status"), "checkpoint": checkpoint,
            "valid_prefix": prefix, "legacy": checkpoint is None,
            "unindexed_data_bytes": max(0, sizes["chunks.npy"] - prefix["data_bytes"]),
            "invalid_index_bytes": max(0, sizes["chunks.jsonl"] - prefix["index_bytes"]),
            "tail_error": tail_error}


def iter_valid_recorded_chunks(path: Path | str):
    """Explicitly read only the inspected valid prefix of a partial recording."""
    path = Path(path)
    report = inspect_recording(path)
    metadata = _read_metadata(path)
    for chunk, _, _, _ in _iter_entries(path, metadata, limit=report["valid_prefix"]["chunk_count"]):
        yield chunk


def recover_recording(path: Path | str, *, expected_session_id: str | None = None,
                      expected_recording_id: str | None = None) -> dict:
    """Mark an inactive writer interrupted, preserving all original arrays/index.

    The OS lease is mandatory: a dead backend may still have a live acquisition
    child. Completed, failed, and previously interrupted metadata are preserved.
    No recovery upgrades a checkpoint or invents metadata for unindexed arrays.
    """
    path = Path(path)
    with StorageLease(path / ".writer.lock"):
        metadata = _read_metadata(path)
        if expected_session_id is not None and metadata.get("session_id") != expected_session_id:
            raise ValueError("Recording metadata belongs to a different session")
        if expected_recording_id is not None and metadata.get("id") != expected_recording_id:
            raise ValueError("Recording metadata has a different recording ID")
        report = inspect_recording(path)
        if metadata.get("status") == "recording":
            metadata["status"] = "interrupted"
            metadata["error"] = "Recording writer exited before finalization"
            metadata["chunk_count"] = report["valid_prefix"]["chunk_count"]
            metadata["sample_count"] = report["valid_prefix"]["sample_count"]
            report["status"] = "interrupted"
            metadata["recovery"] = report
            _write_document(path, metadata)
        prefix = report["valid_prefix"]
        return {key: report[key] for key in ("id", "session_id", "format", "format_version")} | {
            "path": str(path.resolve()), "status": metadata.get("status"),
            "chunk_count": prefix["chunk_count"], "sample_count": prefix["sample_count"],
            "recovery": report, **({"error": metadata["error"]} if "error" in metadata else {}),
            **({"source_recording": deepcopy(metadata["source_recording"])}
               if isinstance(metadata.get("source_recording"), dict) else {}),
        }
