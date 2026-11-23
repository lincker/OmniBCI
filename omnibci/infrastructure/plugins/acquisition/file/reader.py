#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : data_source.py
# @Author   : lincker
# @Date     : 2025-04-17 07:05
from collections.abc import Mapping
import asyncio
from copy import deepcopy
from pathlib import Path

from omnibci.infrastructure.config_paths import resolve_config_path
from omnibci.sdk import SignalSource, SourceCheckpoint, SourceObservation


class FileReader(SignalSource):
    trigger_mode = 'events'
    resume_mode = 'checkpoint'

    def __init__(
        self, *, parameters: Mapping[str, object], resource_root: Path,
    ) -> None:
        super().__init__()
        self.reader_adapter = None
        self._is_streaming = False
        self.file_pointer = 0
        self._file_identity = None
        self._resume_checkpoint = None
        self._read_in_progress = False
        self._chunk_size = parameters.get('file_reader_chunk_size', 100)
        file_path = parameters.get('data_file_path', '')
        if not isinstance(file_path, (str, Path)) or not str(file_path).strip():
            raise ValueError("Select a recording file before starting acquisition")
        self.file_path = str(resolve_config_path(file_path))

    def set_file_path(self, file_path: str | Path):
        if not str(file_path).strip():
            raise ValueError("Select a recording file before starting acquisition")
        self.file_path = str(resolve_config_path(file_path))

    async def connect(self) -> None:
        """Open the signal file."""
        if self.reader_adapter is not None:
            return
        path = Path(self.file_path)
        if not path.is_file():
            raise FileNotFoundError(f"File {self.file_path} does not exist.")
        identity = self._current_file_identity()
        if self.file_path.endswith('.cnt'):
            from .adapter import CNTReaderAdapter
            self.reader_adapter = CNTReaderAdapter(self.file_path)
        else:
            raise ValueError(f"{self.file_path}, Unsupported file format")
        if identity != self._current_file_identity():
            await self.reader_adapter.close()
            self.reader_adapter = None
            raise ValueError("Signal file changed while it was being opened")
        self._file_identity = identity

    async def start_stream(self) -> None:
        """Start at zero, or consume a checkpoint explicitly staged for this start."""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        if self._file_identity is not None:
            self._validate_file_identity(self._file_identity)
        if self._resume_checkpoint is None:
            self.file_pointer = 0
        else:
            self._validate_checkpoint(self._resume_checkpoint)
            self.file_pointer = self._resume_checkpoint.state["next_sample"]
            self._resume_checkpoint = None
        self._is_streaming = True

    async def read_chunk(self):
        """Read one chunk, returning ``None`` at the end of the file."""
        self._read_in_progress = True
        try:
            # Pace before delivery: cancellation must not skip an unreturned chunk.
            await asyncio.sleep(0.01)
            raw_data = await self.reader_adapter.read_chunk(
                start=self.file_pointer, end=self.file_pointer + self._chunk_size,
            )
            if raw_data is None:
                self._is_streaming = False
                return None
            self.file_pointer += raw_data.shape[1]
            # No await between cursor advancement and returning the delivered data.
            return raw_data
        finally:
            self._read_in_progress = False

    def checkpoint(self) -> SourceCheckpoint:
        """Capture the next sample after stop and acquisition-task completion."""
        self._require_stopped()
        if self._file_identity is None or self.reader_adapter is None:
            raise ValueError("Reader must be connected before creating a checkpoint")
        self._validate_file_identity(self._file_identity)
        return SourceCheckpoint(
            resource_id=self._file_identity["path"],
            resource_revision=f'{self._file_identity["size"]}:{self._file_identity["mtime_ns"]}',
            state={"next_sample": self.file_pointer},
        )

    def restore_checkpoint(self, checkpoint: SourceCheckpoint) -> None:
        """Stage a stopped reader to resume once, rejecting a changed source file."""
        self._require_stopped()
        self._validate_checkpoint(checkpoint)
        self._resume_checkpoint = deepcopy(checkpoint)

    def _require_stopped(self) -> None:
        if self._is_streaming or self._read_in_progress:
            raise RuntimeError("Stop and join the acquisition task before using a checkpoint")

    def _current_file_identity(self) -> dict:
        path = Path(self.file_path).resolve(strict=True)
        stat = path.stat()
        return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    def _validate_file_identity(self, expected: dict) -> None:
        if expected != self._current_file_identity():
            raise ValueError("Checkpoint source file differs or has changed")

    def _validate_checkpoint(self, checkpoint: SourceCheckpoint) -> None:
        if (not isinstance(checkpoint, SourceCheckpoint)
                or set(checkpoint.state) != {"next_sample"}
                or type(checkpoint.state["next_sample"]) is not int
                or checkpoint.state["next_sample"] < 0):
            raise ValueError("Invalid file reader checkpoint")
        if self.reader_adapter is None or self._file_identity is None:
            raise ValueError("Reader must be connected before restoring a checkpoint")
        self._validate_file_identity(self._file_identity)
        identity = self._file_identity
        if (checkpoint.resource_id != identity["path"]
                or checkpoint.resource_revision != f'{identity["size"]}:{identity["mtime_ns"]}'):
            raise ValueError("Checkpoint source file differs or has changed")
        if checkpoint.state["next_sample"] > self.reader_adapter.data.shape[1]:
            raise ValueError("Checkpoint sample position exceeds the signal file length")

    async def stop_stream(self) -> None:
        """Stop the file stream, including after partial setup."""
        self._is_streaming = False

    async def disconnect(self) -> None:
        """Close the file and discard the staged cursor."""
        if self.reader_adapter is not None:
            await self.reader_adapter.close()
        self.reader_adapter = None
        self._file_identity = self._resume_checkpoint = None
        self._is_streaming = False

    async def observe(self) -> SourceObservation:
        """Inspect the opened file without consuming signal samples."""
        self._require_stopped()
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        text = self.reader_adapter.get_metadata()
        try:
            info = self.reader_adapter.describe_output()
        except (ValueError, NotImplementedError) as exc:
            return SourceObservation(native_text=text, signal_info=None, native_event="annotations",
                                     provenance="file-reported", layout_error=(str(exc) or "Invalid file layout")[:4096])
        return SourceObservation(native_text=text, signal_info=info,
                                 native_event="annotations", provenance="file-reported")
