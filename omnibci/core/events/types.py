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
    StartStream = 111 # StartStreamData
    StopStream = 112    #
    # neuro engine
    StartNeuroEnginePipeline = 31
    StopNeuroEnginePipeline = 32
    CloseAllNeuroEngineProcessSystem = 33
    # stimulation
    StartStimTrial = 311
    MessageFromStimulation = 9
    StartStimulation = 301
    StopStimulation = 302
    RecognizedResult = 408


class Event:
    '''
    events type is command, stimulator
    '''

    def __init__(self, event_type: EventType = None, data=None) -> None:
        #self.receiver = None
        self.type = event_type
        self.data = data


    def to_json(self):
            return {
                'event_type': self.type,
                'data': self.data
            }
