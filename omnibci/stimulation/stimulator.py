'''
Author: lincker
Date: 2025-05-27 14:47:55
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-11-05 16:41:44
FilePath: /OmniBCI/omnibci/stimulation/stimulator.py
Description: 

Copyright (c) 2025 by lincker, All Rights Reserved. 
'''
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 22:56
import logging
import multiprocessing as mp
import sys, os

from omnibci.core.base.factory import TriggerFactory, StimulusFactory
from omnibci.core.base.acquisition import TriggerBase
from omnibci.core.base.stimulation import StimulusBase
from core.event.base import  EventType, Event
from core.base.factory import TriggerFactory

class Stimulator:
    def __init__(self, stimulus, trigger):
        # super().__init__(group=None, target=None, name=None, args=(), kwargs={})
        # set_high_priority()
        # self.event_manager = event_manager
        self.stimulus = stimulus
        self.trigger = trigger
        # self.trigger = TriggerFactory.get_trigger()
        # if not stimulus:
        #     self.stimulus = SSVEPStimuli()
        # else:
        #     self.stimulus = stimulus
        self.feedback_data = None
        self.should_stop = False

    # def set_stimuli(self, name=None):
    #     if not name:
    #         self.stimulus = SSVEPStimuli()
    #     else:
    #         self._load_stimuli(name)

    def _load_stimuli(self, name): #TODO: load stimuli from file
        pass

    def start(self):
        self.setup()

    def setup(self):
        # self.register_event_listener()
        self.stimulus.setup()
        # self.stimuli.display_init_frame()
    #    self.communication_manager.start()

    def stop(self):
        self.stimulus.teardown()
    #    self.communication_manager.stop()

    def display_feedback(self, result):
        self.stimulus.display_feedback(result)

# TODO： whether use async before def run_command
    def run_command(self, command: Event):
        # TODO：refactor command to CommandType
        if command.type == EventType.StartStimTrial:
            # self.stimuli_running = True
            self.stimulus.play(command.data)
        # if self.stimuli_running and command.type == EventType.FeedbackResult:
        if command.type == EventType.FeedbackResult:
            self.stimulus.display_feedback(command.data)
            print("Stimulator received feedback:", command.data)

    def play(self):
        self.stimulus.play()


class StimulatorProcess(mp.Process):
    def __init__(self):
        super().__init__()
        self.stimulus: StimulusBase = None
        self.trigger: TriggerBase = None

    def run(self):
        self._load_trigger()
        self._load_stimulus()
        self._run()

    def _load_trigger(self):
        from omnibci.core.base.factory import load_plugins
        acquisition_plugin_path = os.path.join(os.path.dirname(__file__), '..', 'plugin', 'acquisition')
        load_plugins(acquisition_plugin_path)
        logging.info("Initializing Trigger in Child Process...")
        self.trigger = TriggerFactory.get_trigger()

    def _load_stimulus(self):
        from omnibci.core.base.factory import load_plugins
        stimulation_plugin_path = os.path.join(os.path.dirname(__file__), '..', 'plugin', 'stimulation')
        load_plugins(stimulation_plugin_path)
        logging.info("Initializing Stimulus in Child Process...")
        self.stimulus = StimulusFactory.get_stimulus()