#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-04-09 10:41

class DataManager:
    def __init__(self, buffer):
        self.buffer = buffer

    def put_data(self, data):
        self.buffer.put_nowait(data)

    def get_data(self):
        return  self.buffer.get()

    def empty(self):
        return self.buffer.empty()

