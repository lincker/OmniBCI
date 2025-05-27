#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-27 15:53
import time
import sys
import asyncio
from enum import IntEnum
import logging
from typing import Iterator

import numpy as np

from acquisition.base import BaseReader, BaseTrigger
from core.data_model import DataPacket
from conf import settings


class Scan45Message:
    class ControlCode(IntEnum):
        CTRL_General = 1
        CTRL_FromServer = 2
        CTRL_FromClient = 3

    class Request(IntEnum):
        # CTRL, code = 1 General Control Code
        RequestVersion = 1
        ClosingUpConnection = 2
        # CTRL, code = 2 Server Control Code
        StartAcquisition = 1
        StopAcquisition = 2
        # CTRL, code = 3 Client Control Code
        RequestEDFHeader = 1  # not implemented
        RequestASTSetupFile = 2  # not implemented
        RequestStartSendingData = 3
        RequestStopSendingData = 4

    class Header:
        HEADER_SIZE = 12

        def __init__(self, ID: str, code: int, request: int, size_body: int):
            self.ID = ID
            self.code = code
            self.request = request
            self.size_body = size_body

        def __str__(self):
            return "".join(str(item) for item in (self.ID, self.code, self.request, self.size_body))

        @classmethod
        def create_from_bytes(cls, header_bytes: bytes):
            if len(header_bytes) != 12:
                print('error! the header length is not 12, header =', header_bytes)
            ID = header_bytes[0:4].decode('ascii')
            code = int.from_bytes(header_bytes[4:6], byteorder='big')
            request = int.from_bytes(header_bytes[6:8], byteorder='big')
            size_body = int.from_bytes(header_bytes[8:12], byteorder='big')
            return cls(ID, code, request, size_body)

        def to_bytes(self):
            ID_bytes = self.ID.encode('ascii')
            code_bytes = self.code.to_bytes(length=2, byteorder='big')
            request_bytes = self.request.to_bytes(length=2, byteorder='big')
            size_body_bytes = self.size_body.to_bytes(length=4, byteorder='big')
            header_bytes = ID_bytes + code_bytes + request_bytes + size_body_bytes
            return header_bytes

    def __init__(self) -> None:
        self.startAcquistionCommand = self._build_start_acquisition_header()
        self.stopAcquistionCommand = self._build_stop_acquisition_header()
        self.startSendingDataCommand = self._build_start_send_data_header()
        self.stopSendingDataCommand = self._build_stop_send_data_header()
        self.getEDFHeaderInfoCommand = self._build_get_EDF_header()
        self.closingUpConnectionCommand = self._build_closing_up_connection_header()

    def _build_start_acquisition_header(self):
        header = self.Header(
            ID='CTRL',
            code=self.ControlCode.CTRL_FromServer,
            request=self.Request.StartAcquisition,
            size_body=0
        )
        # header bytes = b'CTRL\x00\x02\x00\x01\x00\x00\x00\x00'
        return header.to_bytes()

    def _build_stop_acquisition_header(self):
        header = self.Header(
            ID='CTRL',
            code=self.ControlCode.CTRL_FromServer,
            request=self.Request.StopAcquisition,
            size_body=0
        )
        # header bytes = b'CTRL\x00\x02\x00\x02\x00\x00\x00\x00'
        return header.to_bytes()

    def _build_start_send_data_header(self):
        header = self.Header(
            ID='CTRL',
            code=self.ControlCode.CTRL_FromClient,
            request=self.Request.RequestStartSendingData,
            size_body=0
        )
        # header bytes = b'CTRL\x00\x03\x00\x03\x00\x00\x00\x00'
        return header.to_bytes()

    def _build_stop_send_data_header(self):
        header = self.Header(
            ID='CTRL',
            code=self.ControlCode.CTRL_FromClient,
            request=self.Request.RequestStopSendingData,
            size_body=0
        )
        # header bytes = b'CTRL\x00\x03\x00\x04\x00\x00\x00\x00'
        return header.to_bytes()

    def _build_get_EDF_header(self):
        header = self.Header(
            ID='CTRL',
            code=self.ControlCode.CTRL_FromClient,
            request=self.Request.RequestEDFHeader,
            size_body=0
        )
        # header bytes = b'CTRL\x00\x03\x00\x01\x00\x00\x00\x00'
        return header.to_bytes()

    def _build_closing_up_connection_header(self):
        header = self.Header(
            ID='CTRL',
            code=self.ControlCode.CTRL_General,
            request=self.Request.ClosingUpConnection,
            size_body=0
        )
        # header bytes = b'CTRL\x00\x01\x00\x02\x00\x00\x00\x00'
        return header.to_bytes()


scan45_message_manager = Scan45Message()


class Scan45DataReader(BaseReader):

    def __init__(self, config) -> None:
        super().__init__()
        self.data_provider_ip = settings.DATA_PROVIDER_IP
        self.data_provider_port = settings.DATA_PROVIDER_PORT
        # self.n_channels = config.get('n_channels', 9) # refactor later
        # self.record_srate = config.get('record_srate', 1000)
        # self.srate # TODO: refactor later
        # self.info = None
        self.reader = None
        self.writer = None
        self._previous_event = 0 # the integer event position
        self._packets_counter = 0

    async def connect(self) -> bool:
        # TODO: if any connections to scan4.5, disconnect it
        if not self._is_connected:
            await self._connect()
        self._is_connected = True
        return self._is_connected

    # TODO: refactor the return values
    async def _connect(self) -> (asyncio.StreamReader, asyncio.StreamWriter):
        print("connect to ", self.data_provider_ip, self.data_provider_port)
        self.reader, self.writer = await asyncio.open_connection(self.data_provider_ip, self.data_provider_port)
        print("connected")
        return self.reader, self.writer

    async def disconnect(self):
        if self._is_connected:
            stop_message = scan45_message_manager.closingUpConnectionCommand
            self.writer.write(stop_message)
            await self.writer.drain()
            await self._disconnect()
            self._is_connected = False
        return True

    async def _disconnect(self):
        self.writer.close()
        await self.writer.wait_closed()

    async def get_metadata(self):
        await self.request_basic_info()

    async def request_basic_info(self):
        self.writer.write(scan45_message_manager.getEDFHeaderInfoCommand)
        self.writer.drain()
        header, body = await self._load_package()
        # TODO:
        self.info = str(header)

    async def start_stream(self) -> Iterator[DataPacket]:
        self.writer.write(scan45_message_manager.startSendingDataCommand)
        await self.writer.drain()
        self._is_streaming = True
        self._packets_counter = 0
        while self._is_streaming:
            raw_data = await self._read_data()

            # 构造数据包实例
            # import time
            ns = time.time_ns()
            yield DataPacket(
                timestamp=time.time_ns()/1e9,  # 纳秒级时间戳
                data=np.frombuffer(raw_data, dtype=np.float32),
                sequence_id=self._packets_counter,
            )
            self._packets_counter += 1

    # async def _check_quality(self, raw: npt.array) -> int:
    #     rawdata = raw
    #     """异步质量检查（可扩展为CPU密集型操作时用run_in_executor）"""
    #
    #     return DataQualityFlag.OK.value

    async def stop_stream(self) -> bool:
        self.writer.write(scan45_message_manager.stopSendingDataCommand)
        await self.writer.drain()
        self._is_streaming = False
        return True

#   TODO: refactor function name: send_start_acquisition_command start data stream
#     async def _start_data_stream(self):
#         self.writer.write(scan45_message_manager.startSendingDataCommand)

    # async def _stop_data_stream(self):
    #     self.writer.write(scan45_message_manager.stopSendingDataCommand)

    async def _read_data(self):
        # load 1 package data and unpack it
        header, body = await self._load_package()
        if header.ID == 'DATA' and header.code == 2:
            data = self._parse_body_data(header, body)
            # print('data header', header)
            # print('data body', data)
            return data
        else:
            raise Exception('header indicates body does not contain data, not implemented')

    async def _load_package(self):
        header_bytes = await self.reader.read(scan45_message_manager.Header.HEADER_SIZE)
        # header = np.frombuffer(header, dtype=np.uint8)
        header = scan45_message_manager.Header.create_from_bytes(header_bytes=header_bytes)
        if header.size_body == 0:
            print("body size is 0")
        body = await self.reader.read(header.size_body)
        # TODO； check body data type
        return header, body

    def _parse_body_data(self, header: Scan45Message.Header, body: bytes):
        if header.request == 2:
            # 32 bit data
            # data  = np.frombuffer(body, dtype=np.uint32)
            # data = np.frombuffer(body, dtype=np.unit8)
            data = body
            data = self._unpack_int32_eeg(data)
            # data = self._down_sample(data)
            return data
        elif header.request == 1:
            raise '16 bit data parse method not implemented'

    def _unpack_int32_eeg(self, data: bytes):
        # TODO: change n_channels to config file
        n_eeg_channels = 9
        int32_count = int(len(data) / 4)
        iterable = (int.from_bytes(data[4 * x: 4 * x + 4], byteorder='little') if x % 10 != 9 else
                    int.from_bytes(data[4 * x: 4 * x + 1], byteorder='little', signed=False)
                    for x in range(0, int32_count))
        int_data = np.fromiter(iterable, int)
        n_channels = n_eeg_channels + 1  # n_channels = eeg number of channels + 1 event channel
        n_samples = int(len(int_data) / n_channels)
        packet = np.reshape(int_data, (n_channels, n_samples), order='F')
        packet_event = packet[-1, :]
        # remove duplicated event data
        for index, event in enumerate(packet_event):
            # if event != 0:
                # print(f'index = {index}-----------event = {event}')
            if self._previous_event == 0 and event != 0:
                self._previous_event = event
                print(f'index = {index}-----------event = {event}')
            elif self._previous_event != 0:
                if event == self._previous_event:
                    packet_event[index] = 0
                else:
                    self._previous_event = event
        return packet


class Scan45Trigger(BaseTrigger):
    def __init__(self, COM='3100'):
        super().__init__()
        self.COM = COM
        self.address = int(COM, 16)
        self.port = self._set_parallel_port(self.address)
        self.port = None
        self.clear_trigger()

    def send(self, event_id):
        self._send(event_id)

    def _send(self, data):
        if sys.platform == 'win32':
            self.port.setData(data)
        else:
            print(f'macOS do not support parallel port. Send trigger data = {data}')

    def clear_trigger(self):
        self._send(0)

    def _set_parallel_port(self, address):
        if sys.platform == 'win32':
            drivers = dict(inpout32=('_inpout', 'PParallelInpOut'),
                           inpoutx64=('_inpout', 'PParallelInpOut'),
                           dlportio=('_dlportio', 'PParallelDLPortIO'))
            from ctypes import windll
            from importlib import import_module
            ParallelPort = None
            for key, val in drivers.items():
                driver_name, class_name = val
                try:
                    hasattr(windll, key)
                    ParallelPort = getattr(import_module('.' + driver_name, __name__),
                                           class_name)
                    break
                except (OSError, KeyError, NameError):
                    ParallelPort = None
                    continue
            if ParallelPort is not None:
                try:
                    port = ParallelPort(address)
                    return port
                except Exception as e:
                    logging.error(f"Failed to initialize parallel port at {address}: {e}")
                    return None
            if ParallelPort is None:
                logging.warning(
                                "No parallel port driver found. Install either "
                                "inpout32, inpoutx64 or dlportio")