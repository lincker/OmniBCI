#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : file_reader.py
# @Author   : lincker
# @Date     : 2025-04-17 07:05
import asyncio
from pathlib import Path

from omnibci.conf import settings

from omnibci.core.base.acquisition import ReaderBase


# file reader only read the file, the output not include data packet
class FileReader(ReaderBase):
    plugin_type = 'file_reader'

    def __init__(self):
        super().__init__()
        self.reader_adapter = None
        self.file_pointer = 0  # 文件指针，初始位置为0
        self._data_length = 0
        self._chunk_size = settings.get('ACQUISITION.file_reader_chunk_size')
        self.speed = settings.get('ACQUISITION.file_reader_play_speed')
        self.file_path = settings.get('ACQUISITION.data_file_path')
        # self.sample_interval = self.config.get('sample_interval', 0.001)  # 默认采样间隔为1ms
        self.data = None

    def set_file_path(self, file_path: str):
        self.file_path = file_path

    def set_speed(self, speed: float):
        """
        设置回放速度
        :param speed: 回放速度(1.0=正常速度)
        """
        if speed <= 0:
            raise ValueError("Speed must be a positive number.")
        self.speed = speed


    async def connect(self) -> bool:
        """连接到文件"""
        # 根据文件类型选择适当的读取器

        path = Path(self.file_path)
        if not path.exists():
            raise FileNotFoundError(f"File {self.file_path} does not exist.")
        if self.file_path.endswith('.cnt'):
            from .file_adapter import CNTReaderAdapter
            self.reader_adapter = CNTReaderAdapter(self.file_path)
        else:
            raise ValueError(f"{self.file_path}, Unsupported file format")
        self._is_open = True
        return True

    async def start_read(self) -> bool:
        """启动数据流"""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        self.file_pointer = 0  # 重置文件指针
        self._is_streaming = True
        return True

    async def read_unit(self):
        """读取一个数据包"""
        raw_data = await self.reader_adapter.read_chunk(start=self.file_pointer, end=self.file_pointer + self._chunk_size)
        self.file_pointer += self._chunk_size  # 更新文件指针
        if raw_data is None:
            self._is_streaming = False
            raise ValueError("No data read from the file.")
        await asyncio.sleep(0.01)
        return raw_data

    async def stop_read(self) -> bool:

        """停止数据流"""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        # 停止数据流的逻辑
        self._is_streaming = False
        return True

    async def disconnect(self) -> bool:
        """断开连接"""
        if self.reader_adapter is not None:
            # 关闭文件或释放资源
            await self.reader_adapter.close()
            self._is_open = False
        return True

    async def get_metadata(self):
        """获取设备元数据"""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        return self.reader_adapter.get_name()

