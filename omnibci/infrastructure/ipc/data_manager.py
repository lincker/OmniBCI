#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-04-09 10:41
import queue


class DataManager:
    def __init__(self, buffer):
        self.buffer = buffer

    def put_data(self, data):
        self.buffer.put_nowait(data)

    def get_data(self, timeout=None):
        """取一个数据块。

        timeout=None：阻塞直到有数据（原语义，兼容既有调用方）。
        timeout=秒数：阻塞等待至多 timeout 秒，超时返回 None ——
        供 SignalProcessor 的事件化取数使用（P2-7），取代
        empty()+sleep 忙轮询。
        """
        if timeout is None:
            return self.buffer.get()
        try:
            return self.buffer.get(timeout=timeout)
        except queue.Empty:
            return None

    def empty(self):
        return self.buffer.empty()
