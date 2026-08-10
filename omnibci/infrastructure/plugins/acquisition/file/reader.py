#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : data_source.py
# @Author   : lincker
# @Date     : 2025-04-17 07:05
import asyncio
from pathlib import Path

from omnibci.conf import settings
from omnibci.conf.paths import resolve_config_path
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.plugins.registry import register_plugin


@register_plugin(SignalSource)
class FileReader(SignalSource):
    plugin_type = 'file_reader'

    def __init__(self):
        super().__init__()
        self.reader_adapter = None
        self.file_pointer = 0
        self._data_length = 0
        self._chunk_size = settings.get('ACQUISITION.file_reader_chunk_size')
        self.speed = settings.get('ACQUISITION.file_reader_play_speed')
        self.file_path = str(
            resolve_config_path(settings.get('ACQUISITION.data_file_path'))
        )
        self.data = None

    def set_file_path(self, file_path: str | Path):
        self.file_path = str(resolve_config_path(file_path))

    def set_speed(self, speed: float):
        """Set the playback speed, where 1.0 is real time."""
        if speed <= 0:
            raise ValueError("Speed must be a positive number.")
        self.speed = speed

    async def connect(self) -> bool:
        """Open the signal file."""
        path = Path(self.file_path)
        if not path.exists():
            raise FileNotFoundError(f"File {self.file_path} does not exist.")
        if self.file_path.endswith('.cnt'):
            from .adapter import CNTReaderAdapter
            self.reader_adapter = CNTReaderAdapter(self.file_path)
        else:
            raise ValueError(f"{self.file_path}, Unsupported file format")
        self._is_connected = True
        return True

    async def start_stream(self) -> bool:
        """Start reading from the beginning of the file."""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        self.file_pointer = 0
        self._is_streaming = True
        return True

    async def read_chunk(self):
        """Read one chunk, returning ``None`` at the end of the file."""
        raw_data = await self.reader_adapter.read_chunk(start=self.file_pointer, end=self.file_pointer + self._chunk_size)
        if raw_data is None:
            self._is_streaming = False
            return None
        self.file_pointer += self._chunk_size
        await asyncio.sleep(0.01)
        return raw_data

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
