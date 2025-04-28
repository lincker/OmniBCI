#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum


class EventType(IntEnum):
    # ConnectDataProvider = 101
    # DisconnectDataProvider = 102
    # GetAcquisitionDeviceStatus = 103
    # StopAcquisitionDevice = 104
    StartReceive = 21 # StartReadData
    StopReceive = 22    #
    StartProcessSystem = 31
    StopProcessSystem = 32
    CloseAllProcessSystem = 33
    StopAnalysis = 34
    StartStimTrail = 41
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


def event_obj_to_json(obj: Event):
        return {
            'type': obj.type,
            'data': obj.data
        }


def event_from_json(json_str):
    return Event(type=json_str['type'], data=json_str['data'])
