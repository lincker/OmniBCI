"""Paced replay of recorded raw channels with explicit checkpoint resumption."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

from omnibci.conf.paths import resolve_config_path
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.domain.signal_info import SignalInfo, fingerprint
from omnibci.infrastructure.acquisition.recording_reader import open_recording_reader
from omnibci.sdk import PluginContext, PluginParameters


class RecordingReplay(SignalSource):
    plugin_type = "recording_replay"

    def __init__(self, *, parameters: PluginParameters, context: PluginContext | None = None):
        super().__init__()
        path = parameters.get("recording_path", "")
        if not isinstance(path, (str, Path)) or not str(path).strip():
            raise ValueError("Select a recording directory or metadata.json file before starting acquisition")
        self.path = resolve_config_path(path)
        self.allow_partial = parameters.get("allow_partial", False)
        if type(self.allow_partial) is not bool:
            raise ValueError("allow_partial must be a boolean")
        self._reader = None
        self._chunks = None
        self._pending_chunk = None
        self._next_chunk = 0
        self._resume_checkpoint = None
        self._read_in_progress = False

    async def connect(self) -> bool:
        if self._reader is None:
            self._reader = open_recording_reader(self.path, allow_partial=self.allow_partial)
        else:
            self._reader.validate_identity()
        self._is_connected = True
        return True

    async def start_stream(self) -> bool:
        reader = self._connected_reader()
        reader.validate_identity()
        if self._is_streaming:
            return True
        self._require_stopped()
        position = 0
        if self._resume_checkpoint is not None:
            self._validate_checkpoint(self._resume_checkpoint)
            position = self._resume_checkpoint["next_chunk"]
        self._close_chunks()
        self._chunks = reader.chunks(position)
        self._next_chunk = position
        self._resume_checkpoint = None
        self._is_streaming = True
        return True

    async def read_chunk(self):
        reader = self._connected_reader()
        if self._read_in_progress:
            raise RuntimeError("A recording read is already in progress")
        if not self._is_streaming:
            return None
        self._read_in_progress = True
        try:
            reader.validate_identity()
            if self._pending_chunk is None:
                self._pending_chunk = next(self._chunks, None)
            if self._pending_chunk is None:
                self._is_streaming = False
                return None
            raw = self._pending_chunk.raw_data
            # Pace before delivery so cancellation never advances the checkpoint.
            await asyncio.sleep(raw.shape[1] / reader.signal_info.sample_rate)
            if not self._is_streaming:
                return None
            reader.validate_identity()
            self._next_chunk += 1
            self._pending_chunk = None
            return raw
        finally:
            self._read_in_progress = False

    async def stop_stream(self) -> bool:
        self._is_streaming = False
        return True

    async def disconnect(self) -> bool:
        self._require_stopped()
        self._close_chunks()
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._is_connected = False
        return True

    async def get_metadata(self) -> str:
        reader = self._connected_reader()
        return json.dumps({"source_recording": self.recording_provenance,
                           "signal_info": reader.signal_info.to_dict()}, indent=2)

    def describe_output(self, *, source_id: str, connection_id: str) -> SignalInfo:
        return replace(self._connected_reader().signal_info, source_id=source_id,
                       connection_id=connection_id, provenance="file-reported")

    @property
    def recording_provenance(self) -> dict | None:
        return deepcopy(self._reader.provenance) if self._reader is not None else None

    @property
    def supports_checkpoint(self) -> bool:
        return True

    def checkpoint(self) -> dict:
        self._require_stopped()
        reader = self._connected_reader()
        reader.validate_identity()
        return {
            "format_version": 1, "source": self.plugin_type,
            "recording": deepcopy(reader.identity), "next_chunk": self._next_chunk,
            "source_identity": {"resource_id": reader.provenance["path"],
                                "revision": fingerprint(reader.identity)},
        }

    def restore_checkpoint(self, checkpoint: dict) -> None:
        self._require_stopped()
        self._validate_checkpoint(checkpoint)
        self._resume_checkpoint = deepcopy(checkpoint)

    def _validate_checkpoint(self, checkpoint: dict) -> None:
        reader = self._connected_reader()
        reader.validate_identity()
        if (not isinstance(checkpoint, dict) or type(checkpoint.get("format_version")) is not int
                or checkpoint["format_version"] != 1 or checkpoint.get("source") != self.plugin_type
                or type(checkpoint.get("next_chunk")) is not int
                or not 0 <= checkpoint["next_chunk"] <= reader.chunk_count
                or checkpoint.get("recording") != reader.identity):
            raise ValueError("Invalid checkpoint or recording files differ")

    def _connected_reader(self):
        if self._reader is None or not self._is_connected:
            raise RuntimeError("Connect the recording before reading or using a checkpoint")
        return self._reader

    def _require_stopped(self) -> None:
        if self._is_streaming or self._read_in_progress:
            raise RuntimeError("Stop and join the acquisition task before using a checkpoint")

    def _close_chunks(self) -> None:
        if self._chunks is not None:
            self._chunks.close()
            self._chunks = None
        self._pending_chunk = None
