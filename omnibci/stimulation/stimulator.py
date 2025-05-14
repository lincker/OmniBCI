#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 22:56
import sys, os
import time
import multiprocessing as mp
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from stimulation.stimuli import SSVEPStimuli
from core.event.base import  EventType, Event


class Stimulator:
    # TODO: refactor, remove event manager. use run command, command can have many types: receive data, start trial
    def __init__(self, config=None, stimuli=None):
        # super().__init__(group=None, target=None, name=None, args=(), kwargs={})
        # set_high_priority()
        # self.event_manager = event_manager
        if not stimuli:
            self.stimuli = SSVEPStimuli()
        else:
            self.stimuli = stimuli
        self.feedback_data = None
        self.should_stop = False

    def set_stimuli(self, name=None):
        if not name:
            self.stimuli = SSVEPStimuli()
        else:
            self._load_stimuli(name)

    def _load_stimuli(self, name): #TODO: load stimuli from file
        pass

    def start(self):
        self.setup()

    def setup(self):
        # self.register_event_listener()
        self.stimuli.setup()
        self.stimuli.display_init_frame()
    #    self.communication_manager.start()

    def stop(self):
        self.stimuli.teardown()
    #    self.communication_manager.stop()

# TODO： whether use async before def run_command
    def run_command(self, command: Event):
        # TODO：refactor command to CommandType
        if command.type == EventType.StartStimTrail:
            self.stimuli.trial_play()
        elif command.type == EventType.FeedbackResult:
            result = command.data
            self.stimuli.display_feedback(result)
