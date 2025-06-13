#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-04-09 10:41
import asyncio


class DataManager:
    def __init__(self, data_buffer=None):
        self.data_buffer = data_buffer if data_buffer is not None else asyncio.Queue()

    def put_packet(self, packet):
        self.data_buffer.put_nowait(packet)

    def get_packet(self):
        return self.data_buffer.get()

    def empty(self):
        return self.data_buffer.empty()

