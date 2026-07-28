#!/usr/bin/env python
# -*- coding: utf-8 -*-
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
    async def read_chunk(self, start: int, end: int):
        raise NotImplementedError("read_data() must be implemented in subclasses")

    @abstractmethod
    async def close(self):
        pass


class CNTReaderAdapter(ReaderAdapter):
    """CNT文件读取器"""

    def __init__(self, file_path):
        super().__init__(file_path)
        self.file = mne.io.read_raw_cnt(input_fname=self.file_path, data_format='auto', preload=True, date_format='mm/dd/yy')
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

    async def read_chunk(self, start: int, end: int):
        # EOF: numpy 越界切片返回空数组而非 None，上游 FileReader 以
        # `raw_data is None` 判断流结束，因此这里显式返回 None。
        if start >= self.data.shape[1]:
            return None
        chunk = self.data[:, start:end]
        if chunk.shape[1] == 0:
            return None
        return chunk

    async def close(self):
        self.file.close()
