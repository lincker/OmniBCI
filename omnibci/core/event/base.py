#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum


class EventType(IntEnum):
    ConnectDataProvider = 101
    DisconnectDataProvider = 102
    # GetDataProviderMetaData = 103
    # StopAcquisitionDevice = 104
    StartReceive = 111 # StartReadData
    StopReceive = 112    #
    StartProcessSystem = 31
    StopProcessSystem = 32
    CloseAllProcessSystem = 33
    StopAnalysis = 34
    StartStimTrial = 41
    FeedbackResult = 8
    MessageFromStimulation = 9
    StartStimulation = 301
    StopStimulation = 302


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
