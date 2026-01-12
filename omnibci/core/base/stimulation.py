#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 11:22
from abc import ABC, abstractmethod
from typing import Dict, Type

from omnibci.core.base.registry import Registerable


STIMULUS_REGISTRY: Dict[str, Type['StimulusBase']] = {}

class StimulusBase(Registerable, ABC):
    _target_registry = STIMULUS_REGISTRY

    @abstractmethod
    def setup(self):
        pass
    @abstractmethod
    def teardown(self):
        pass

    @abstractmethod
    def play(self, *args, **kwargs) -> None: pass
