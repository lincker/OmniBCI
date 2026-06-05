#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 11:22
from abc import ABC, abstractmethod
from typing import Dict, Type, ClassVar

from omnibci.shared.registry import Registerable


class ParadigmBase(Registerable, ABC):
    _target_registry: ClassVar[Dict[str, Type['ParadigmBase']]] = {}

    @abstractmethod
    def setup(self):
        pass
    @abstractmethod
    def teardown(self):
        pass

    @abstractmethod
    def trial_stimulus_present(self, event_id) -> None: pass

    @abstractmethod
    def trial_result_present(self, result) -> None: pass
