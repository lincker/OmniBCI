#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker
# @Date     : 2025-04-13 02:04
from abc import ABC, abstractmethod

import mne
import numpy as np

from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor
from omnibci.infrastructure.acquisition.metadata import acquisition_descriptor


class ReaderAdapter(ABC):
    """File reader interface."""

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path

    @abstractmethod
    async def read_chunk(self, start: int, end: int):
        raise NotImplementedError("read_data() must be implemented in subclasses")

    @abstractmethod
    async def close(self):
        pass


class CNTReaderAdapter(ReaderAdapter):
    """CNT file reader."""

    def __init__(self, file_path):
        super().__init__(file_path)
        self.file = mne.io.read_raw_cnt(input_fname=self.file_path, data_format='auto', preload=True, date_format='mm/dd/yy')
        self._metadata = (f"{self.file.info}\n\nChannel labels: {', '.join(self.file.ch_names)}"
                          f"\n\n{self.file.annotations}")
        # Match the existing raw['data'] selection, including its bad-row exclusion.
        self.file.pick("data", exclude="bads")
        self._set_event()

    def _set_event(self):
        self.data = self.file['data'][0]
        events, event_dict = mne.events_from_annotations(self.file)
        event_channel_length = self.data.shape[1]
        event_channel = np.zeros(event_channel_length)
        for position, _, value in events:
            event_channel[position] = value
            print(f'CNTReaderAdapter: set event: position = {position}, event = {value}')
        arr = np.vstack((self.data, event_channel))
        self.data = arr

    def get_metadata(self) -> str:
        """Return file information captured before selecting output channels."""
        return self._metadata

    def describe_output(self, *, source_id: str, connection_id: str) -> AcquisitionDescriptor:
        return acquisition_descriptor({
            "channel_count": len(self.file.ch_names),
            "output_channel_count": len(self.file.ch_names) + 1,
            "sample_rate": self.file.info["sfreq"],
            "channel_labels": list(self.file.ch_names),
            "channel_types": self.file.get_channel_types(),
            "channel_units": ["V" if channel["unit"] == 107 else None
                              for channel in self.file.info["chs"]],
            "native_event": "annotations",
            "provenance": "file-reported",
        }, source_id=source_id, connection_id=connection_id)

    async def read_chunk(self, start: int, end: int):
        # FileReader recognizes None as EOF; NumPy would return an empty slice.
        if start >= self.data.shape[1]:
            return None
        chunk = self.data[:, start:end]
        if chunk.shape[1] == 0:
            return None
        return chunk

    async def close(self):
        self.file.close()
