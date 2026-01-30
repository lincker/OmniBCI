#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 11:22


# the devices controlled by BCI.

from abc import ABC, abstractmethod
from typing import Dict, Type, ClassVar

from omnibci.core.utils.registry import Registerable

DEVICE_REGISTRY: Dict[str, Type['DeviceBase']] = {}


class PeripheralBase(Registerable, ABC):
    _target_registry: ClassVar[Dict[str, Type['PeripheralBase']]] = {}

    @abstractmethod
    def setup(self):
        pass

    @abstractmethod
    def teardown(self):
        pass

    @abstractmethod
    def play(self, *args, **kwargs) -> None: pass
