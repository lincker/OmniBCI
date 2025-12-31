#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-04-09 10:41
import asyncio
from .data_model import StreamFrame

class DataManager:
    def __init__(self, data_buffer=None):
        self.data_buffer = data_buffer if data_buffer is not None else asyncio.Queue()

    def put(self, data: StreamFrame):
        self.data_buffer.put_nowait(data)

    def get(self) -> StreamFrame:
        return self.data_buffer.get()

    def empty(self):
        return self.data_buffer.empty()

