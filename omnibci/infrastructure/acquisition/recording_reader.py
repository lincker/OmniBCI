"""Read recorded signals through an explicit format/version boundary."""

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Protocol

from omnibci.domain.signal import SignalChunk
from omnibci.domain.signal_info import SignalInfo
from omnibci.infrastructure.acquisition.recording import (
    FORMAT, FORMAT_VERSION, inspect_recording, iter_recorded_chunks, iter_valid_recorded_chunks,
)
from omnibci.infrastructure.storage_lock import StorageLease


class RecordedSignalReader(Protocol):
    signal_info: SignalInfo
    chunk_count: int
    sample_count: int
    identity: dict
    provenance: dict

    def chunks(self, start: int = 0) -> Iterator[SignalChunk]: ...

    def validate_identity(self) -> None: ...

    def close(self) -> None: ...


def open_recording_reader(path: Path | str, *, allow_partial: bool = False) -> RecordedSignalReader:
    """Choose a reader without exposing format details to acquisition plugins."""
    path = Path(path).expanduser().resolve(strict=True)
    if path.is_file():
        if path.name != "metadata.json":
            raise ValueError("Select a recording directory or its metadata.json file")
        path = path.parent
    metadata = _metadata(path)
    format_id, version = metadata.get("format", FORMAT), metadata.get("format_version")
    if format_id == FORMAT and type(version) is int and version == FORMAT_VERSION:
        return NumpyRecordingReader(path, allow_partial=allow_partial)
    raise ValueError(f"Unsupported recording format/version: {format_id!r}/{version!r}")


def _metadata(path: Path) -> dict:
    with (path / "metadata.json").open(encoding="utf-8") as stream:
        metadata = json.load(stream)
    if not isinstance(metadata, dict):
        raise ValueError("Recording metadata must be an object")
    return metadata


class NumpyRecordingReader:
    """Validate a fixed recording while holding its writer/recovery lease."""

    def __init__(self, path: Path, *, allow_partial: bool):
        self.path = path
        self._lease = StorageLease(path / ".writer.lock")
        try:
            self.identity = self._identity()
            metadata = _metadata(path)
            status = metadata.get("status")
            if status != "completed" and (not allow_partial or status not in {"interrupted", "failed"}):
                raise ValueError("Replay requires a completed recording or explicit permission for an inactive partial recording")
            for field in ("id", "session_id"):
                if not isinstance(metadata.get(field), str) or not metadata[field].strip():
                    raise ValueError(f"Recording requires a nonempty {field}")
            self.signal_info = SignalInfo.from_dict(metadata.get("signal_info"))
            report = inspect_recording(path)
            if status == "completed" and (report["unindexed_data_bytes"] or report["invalid_index_bytes"]):
                raise ValueError("Completed recording contains data outside its index")
            self.partial = status != "completed"
            self.chunk_count = report["valid_prefix"]["chunk_count"]
            self.sample_count = report["valid_prefix"]["sample_count"]
            self.provenance = {
                "recording_id": metadata["id"], "session_id": metadata["session_id"],
                "path": str(path), "format": report["format"], "format_version": report["format_version"],
                "status": status, "partial": self.partial,
                "chunk_count": self.chunk_count, "sample_count": self.sample_count,
            }
            self.validate_identity()
        except BaseException:
            self.close()
            raise

    def chunks(self, start: int = 0) -> Iterator[SignalChunk]:
        self.validate_identity()
        reader = iter_valid_recorded_chunks if self.partial else iter_recorded_chunks
        for index, chunk in enumerate(reader(self.path)):
            self.validate_identity()
            if index >= start:
                yield chunk

    def _identity(self) -> dict:
        files = {}
        for name in ("metadata.json", "chunks.npy", "chunks.jsonl"):
            stat = (self.path / name).stat()
            files[name] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                           "ctime_ns": stat.st_ctime_ns, "inode": stat.st_ino}
        return {"path": str(self.path), "files": files}

    def validate_identity(self) -> None:
        if self._lease is None:
            raise RuntimeError("Recording reader is closed")
        if self.identity != self._identity():
            raise ValueError("Recording files differ or have changed")

    def close(self) -> None:
        if self._lease is not None:
            self._lease.close()
            self._lease = None
