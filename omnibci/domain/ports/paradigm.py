#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-01-04 11:22
from abc import ABC, abstractmethod
from typing import Dict, Type, ClassVar

from omnibci.shared.registry import Registerable


class Paradigm(Registerable, ABC):
    _target_registry: ClassVar[Dict[str, Type['Paradigm']]] = {}

    @abstractmethod
    def setup(self):
        pass
    @abstractmethod
    def teardown(self):
        pass

    @abstractmethod
    def trial_stimulus_present(self, params) -> None:
        """Present one trial.

        *params* is this paradigm's per-trial parameter, opaque to the
        dispatch/queue layer.  Each paradigm defines its own shape (e.g. SSVEP:
        an int target/event id; P300: a flash-sequence spec; MI: a cue class).
        """
        pass

    @abstractmethod
    def trial_result_present(self, result) -> None: pass
