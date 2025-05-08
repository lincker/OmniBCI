#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : __init__.py.py
# @Author   : lincker
# @Date     : 2025-04-13 02:04
from abc import ABC, abstractmethod
import mne
import numpy as np


class ReaderAdapter(ABC):
    """文件读取器接口"""

    def __init__(self, file_path):
        super().__init__()
        self.file_path = file_path

    @abstractmethod
    async def read_data(self, start: int = 0, end: int = None):
        raise NotImplementedError("read_data() must be implemented in subclasses")

    @abstractmethod
    async def close(self):
        pass


class CNTReaderAdapter(ReaderAdapter):
    """CNT文件读取器"""

    def __init__(self, file_path):
        super().__init__(file_path)
        self.file = mne.io.read_raw_cnt(self.file_path, preload=True)
        self._set_event()

    def _set_event(self):
        self.data = self.file['data'][0]
        events, event_dict = mne.events_from_annotations(self.file)
        self.data_length = self.data.shape[1]
        event_channel = np.zeros(self.data_length)
        for position, _, value in events:
            event_channel[position] = value
        arr = np.vstack((self.data, event_channel))
        self.data = arr

    async def read_data(self, start: int = 0, end: int = None):
        return self.data[:, start:end]

    async def close(self):
        self.file.close()