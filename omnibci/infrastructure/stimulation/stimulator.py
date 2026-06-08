#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 22:56
import os

import omnibci
from omnibci.domain.ports.acquisition import TriggerEmitter
from omnibci.domain.ports.paradigm import Paradigm

class Stimulator:
    def __init__(self, ):
        self.paradigm: Paradigm
        self.trigger: TriggerEmitter
        self.should_stop = False

    def _load_trigger(self):
        acquisition_plugin_path = os.path.join(next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'acquisition')
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory
        PluginLoader.load_from_dir(acquisition_plugin_path, base_package='omnibci.infrastructure.plugins.acquisition')
        trigger = ObjectFactory.create(TriggerEmitter, config_key='ACQUISITION.trigger')
        return trigger

    def _load_paradigm(self, trigger: TriggerEmitter):
        paradigm_plugin_path = os.path.join(next(iter(omnibci.__path__)), 'infrastructure', 'plugins', 'paradigms')
        from omnibci.shared.loader import PluginLoader
        from omnibci.shared.factory import ObjectFactory
        PluginLoader.load_from_dir(paradigm_plugin_path, base_package='omnibci.infrastructure.plugins.paradigms')
        paradigm = ObjectFactory.create(Paradigm, config_key='STIMULATION.paradigm', trigger=trigger)
        return paradigm

    def start(self):
        self.trigger = self._load_trigger()
        self.paradigm = self._load_paradigm(trigger=self.trigger)
        self.paradigm.setup()

    def stop(self):
        self.paradigm.teardown()

    def present_trial(self, event_id):
        self.paradigm.trial_stimulus_present(event_id=event_id)

    def present_result(self, result):
        self.paradigm.trial_result_present(result=result)
