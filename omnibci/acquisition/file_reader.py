#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : file_reader.py
# @Author   : lincker
# @Date     : 2025-04-17 07:05
import asyncio

from .base import BaseReader
from core.data_model import DataPacket
from omnibci.conf import settings

# file reader only read the file, the output not include data packet
class FileReader(BaseReader):

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.reader_adapter = None
        self.file_pointer = 0  # 文件指针，初始位置为0
        self._data_length = 0
        self._chunk_size = settings.FILE_READER_CHUNK_SIZE
        self.speed = settings.FILE_READER_PLAY_SPEED
        self.file_path = self.config.get('file_path', '')
        # self.sample_interval = self.config.get('sample_interval', 0.001)  # 默认采样间隔为1ms
        self.data = None
        self._is_streaming = False

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
        if self.file_path.endswith('.cnt'):
            from .file_adapters import CNTReaderAdapter
            self.reader_adapter = CNTReaderAdapter(self.file_path)
        else:
            raise ValueError(f"{self.file_path}, Unsupported file format")
        return True

    async def start_stream(self) -> bool:
        """启动数据流"""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        self.file_pointer = 0  # 重置文件指针
        self._is_streaming = True
        return True

    async def read_data(self):
        """读取一个数据包"""
        raw_data = await self.reader_adapter.read_data(start=self.file_pointer, end=self.file_pointer + self._chunk_size)
        self.file_pointer += self._chunk_size  # 更新文件指针
        if raw_data is None:
            self._is_streaming = False
            raise ValueError("No data read from the file.")
        await asyncio.sleep(0.01)
        return raw_data

        # TODO: delete
        # packet = DataPacket(
        #     timestamp=time.time_ns(),
        #     data=raw_data,
        #     sequence_id=self.file_pointer // self._chunk_size,
        # )
        # self.file_pointer += self._chunk_size
        # return packet



    # async def start_stream(self) -> AsyncIterator[DataPacket]:
    #     """开始数据流"""
    #     if self.reader_adapter is None:
    #         raise ValueError("Reader not initialized. Call connect() first.")
    #     self._is_streaming = True
    #     packets_counter = 0
    #     file_pointer = 0
    #     #TODO: replay speed and timestamp
    #     # time_scaler = 1.0 / self.speed
    #     # base_ts = time.time_ns()
    #     # file_ts = 0
    #
    #     while self._is_streaming:
    #         # expected_ts = (time.time_ns() - base_ts) * time_scaler / 1e9
    #         # if file_ts > expected_ts + 0.1:  # 超过100ms则等待
    #         #     await asyncio.sleep(0.01)
    #         #     continue
    #
    #         raw_data = await self.reader_adapter.read_data(file_pointer, file_pointer + self._chunk_size)
    #         file_pointer += self._chunk_size
    #         if raw_data is None:
    #             self._is_streaming = False
    #             raise ValueError("No data read from the file.")
    #         packet = DataPacket(
    #             timestamp=time.time_ns(),
    #             data=raw_data,
    #             sequence_id=packets_counter,
    #         )
    #         # file_ts += self.sample_interval
    #         packets_counter += 1
    #
    #         yield packet



    async def stop_stream(self) -> bool:

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
        return True

    async def get_metadata(self):
        """获取设备元数据"""
        if self.reader_adapter is None:
            raise ValueError("Reader not initialized. Call connect() first.")
        return self.reader_adapter.get_metadata()

