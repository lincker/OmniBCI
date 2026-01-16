#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 11:22


# the device controlled by BCI.

from abc import ABC, abstractmethod
from typing import Dict, Type

from omnibci.core.base.registry import Registerable

DEVICE_REGISTRY: Dict[str, Type['DeviceBase']] = {}


class DeviceBase(Registerable, ABC):
    _target_registry = DEVICE_REGISTRY

    @abstractmethod
    def setup(self):
        pass

    @abstractmethod
    def teardown(self):
        pass

    @abstractmethod
    def play(self, *args, **kwargs) -> None: pass
