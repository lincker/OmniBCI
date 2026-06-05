#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum


class EventType(IntEnum):
    # acquisition
    ConnectDataProvider = 101
    DisconnectDataProvider = 102
    StartStream = 111
    StopStream = 112
    # neuro engine
    StartNeuroEngineSignalProcessor = 31
    StopNeuroEngineSignalProcessor = 32
    CloseAllNeuroEngineProcessSystem = 33
    # stimulation
    StartStimTrial = 311
    StimulationAck = 312   # stim → engine: ack/result of a stimulation command
    MessageFromStimulation = 9
    StartStimulation = 301
    StopStimulation = 302
    # --- domain events (carry rich domain objects as payload) ---------
    TrialCompleted = 501   # payload: Classification VO
    SessionStarted = 502   # payload: session_id str
    SessionEnded   = 503   # payload: total_trials int


class Event:
    '''
    events type is command, stimulator
    '''

    def __init__(self, event_type: EventType = None, data=None) -> None:
        #self.receiver = None
        self.type:EventType = event_type
        self.data:int = data


    def to_json(self):
            return {
                'type': self.type,
                'data': self.data
            }

    @classmethod
    def create_from_json(cls, event_json):
        return cls(**event_json)

