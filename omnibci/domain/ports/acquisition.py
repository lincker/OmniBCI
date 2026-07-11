"""
Author: lincker
Date: 2026-01-08 14:52:39
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2026-04-23 11:36:41
FilePath: /OmniBCI/omnibci/interfaces/acquisition.py
Description:

Copyright (c) 2026 by lincker, All Rights Reserved.
"""
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-12-31 10:34
from abc import ABC, abstractmethod
from typing import Any

from numpy import typing as npt

class SignalSource(ABC):
    """
    Abstract class for signal source.

    This class defines the interface for signal source, which are responsible for
    reading data from a source and providing it in a standardized format.
    """
    def __init__(self):
        super().__init__()
        self._is_connected = False
        self._is_streaming = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @abstractmethod
    async def get_metadata(self):
        """get metadata"""

    @abstractmethod
    async def connect(self) -> bool:
        """setup connection"""

    @abstractmethod
    async def start_stream(self) -> bool:
        """start reading data"""

    @abstractmethod
    async def read_chunk(self)-> npt.NDArray[Any]:
        """Read a fixed sized chunk.

        Returns None to signal a clean end of stream (e.g. file playback
        reached EOF); live device sources simply never return None.
        """

    @abstractmethod
    async def stop_stream(self) -> bool:
        """stop reading data"""

    @abstractmethod
    async def disconnect(self) -> bool:
        """teardown connection"""


class TriggerEmitter(ABC):
    @abstractmethod
    def send(self, event_id: int) -> bool:
        """send trigger/event"""

    @abstractmethod
    def clear(self) -> None:
        """clear trigger/event, get read for sending event next time"""
