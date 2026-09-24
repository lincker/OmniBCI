#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : data_source.py
# @Author   : lincker
# @Date     : 2025-04-17 07:05
import asyncio
from copy import deepcopy
from pathlib import Path

from omnibci.conf import settings
from omnibci.conf.paths import resolve_config_path
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.sdk import PluginContext, PluginParameters


class FileReader(SignalSource):
    plugin_type = 'file_reader'

    def __init__(
        self, *, parameters: PluginParameters | None = None,
        context: PluginContext | None = None,
    ):
        super().__init__()
        self.reader_adapter = None
        self.file_pointer = 0
        self._data_length = 0
        self._file_identity = None
        self._resume_checkpoint = None
        self._read_in_progress = False
        if parameters is None and context is None:
            self._chunk_size = settings.get('ACQUISITION.file_reader_chunk_size')
            file_path = settings.get('ACQUISITION.data_file_path')
        else:
            parameters = parameters if parameters is not None else {}
            self._chunk_size = parameters.get('file_reader_chunk_size', 100)
            file_path = parameters.get('data_file_path', 'tests/fixture/train.cnt')
        self.file_path = str(resolve_config_path(file_path))
        self.data = None

    def set_file_path(self, file_path: str | Path):
        self.file_path = str(resolve_config_path(file_path))

    async def connect(self) -> bool:
        """Open the signal file."""
        path = Path(self.file_path)
        if not path.exists():
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
        self._is_connected = True
        return True

    async def start_stream(self) -> bool:
        """Start at zero, or consume a checkpoint explicitly staged for this start."""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        if self._file_identity is not None:
            self._validate_file_identity(self._file_identity)
        if self._resume_checkpoint is None:
            self.file_pointer = 0
        else:
            self._validate_checkpoint(self._resume_checkpoint)
            self.file_pointer = self._resume_checkpoint["next_sample"]
            self._resume_checkpoint = None
        self._is_streaming = True
        return True

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

    @property
    def supports_checkpoint(self) -> bool:
        return True

    def checkpoint(self) -> dict:
        """Capture the next sample after stop and acquisition-task completion."""
        self._require_stopped()
        if self._file_identity is None:
            raise ValueError("Reader must be connected before creating a checkpoint")
        self._validate_file_identity(self._file_identity)
        return {
            "format_version": 1,
            "source": self.plugin_type,
            "file": deepcopy(self._file_identity),
            "source_identity": {
                "resource_id": self._file_identity["path"],
                "revision": f'{self._file_identity["size"]}:{self._file_identity["mtime_ns"]}',
            },
            "next_sample": self.file_pointer,
        }

    def restore_checkpoint(self, checkpoint: dict) -> None:
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

    def _validate_checkpoint(self, checkpoint: dict) -> None:
        if (
            not isinstance(checkpoint, dict)
            or type(checkpoint.get("format_version")) is not int
            or checkpoint["format_version"] != 1
            or checkpoint.get("source") != self.plugin_type
            or type(checkpoint.get("next_sample")) is not int
            or checkpoint["next_sample"] < 0
            or not isinstance(checkpoint.get("file"), dict)
        ):
            raise ValueError("Invalid file reader checkpoint")
        self._validate_file_identity(checkpoint["file"])
        if self._file_identity is not None and checkpoint["file"] != self._file_identity:
            raise ValueError("Checkpoint does not match the opened signal file")
        data = getattr(self.reader_adapter, "data", None)
        if data is not None and checkpoint["next_sample"] > data.shape[1]:
            raise ValueError("Checkpoint sample position exceeds the signal file length")

    async def stop_stream(self) -> bool:
        """Stop the file stream."""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        self._is_streaming = False
        return True

    async def disconnect(self) -> bool:
        """Close the file."""
        if self.reader_adapter is not None:
            await self.reader_adapter.close()
            self._is_connected = False
        return True

    async def get_metadata(self):
        """Return metadata from the reader adapter."""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        return self.reader_adapter.get_metadata()
