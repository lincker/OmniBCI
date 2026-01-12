#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum


class EventType(IntEnum):
    # acquisition
    ConnectDataProvider = 101
    DisconnectDataProvider = 102
    # GetDataProviderMetaData = 103
    # StopAcquisitionDevice = 104
    StartRead = 111 # StartReadData
    StopRead = 112    #
    # neuro engine
    StartNeuroEnginePipeline = 31
    StopNeuroEnginePipeline = 32
    CloseAllNeuroEngineProcessSystem = 33
    # stimulation
    StartStimTrial = 311
    MessageFromStimulation = 9
    StartStimulation = 301
    StopStimulation = 302
    FeedbackResult = 408


class Event:
    '''
    event type is command, stimulator
    '''

    def __init__(self, type: EventType = None, data=None) -> None:
        #self.receiver = None
        self.type = type
        self.data = data


    def to_json(self):
            return {
                'type': self.type,
                'data': self.data
            }
