#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum, unique
from typing import Any


@unique
class EventType(IntEnum):
    # acquisition
    ConnectDataProvider = 101
    DisconnectDataProvider = 102
    GetSourceCheckpoint = 105
    RestoreSourceCheckpoint = 106
    GetSourceMetadata = 108
    StartStream = 111
    StopAndDrain = 113  # acquisition stop followed by an ordered processing barrier
    StartAcquisitionPreview = 114
    GetAcquisitionPreview = 116
    AttachProcessingInput = 117
    DetachProcessingInput = 118
    StageProcessingConfiguration = 211  # stage a pipeline until the next epoch boundary
    CancelProcessingConfiguration = 212  # reconcile or cancel a staged pipeline
    PrepareSignalProcessing = 214
    ResetProcessingInput = 215
    ProcessingInputFault = 216  # acquisition delivery failed; source reading continues
    AcquisitionState = 121  # worker -> API: authoritative device-state snapshot
                            # payload: application.events.AcquisitionSnapshot
    CommandResult = 601      # command consumer -> API: application.events.CommandResult
    PeerDeliveryFault = 602  # local channel -> owner: delivery stopped, not an execution receipt
    PeerHello = 701          # external peer -> engine: pinned runtime identity
    PeerAccepted = 702       # engine -> external peer: matching identity accepted
    # stimulation
    StartStimTrial = 311
    StimulationAck = 312   # stim → engine: ack/result of a stimulation command
    StopStimulation = 302
    PauseStimulation = 303  # correlated renderer stop and trigger clear
    # actuation (external-device control; the actuation subsystem loads
    # an Effector plugin to carry out these commands)
    ActuationCommand = 401  # engine → actuation: drive the external device
    ActuationAck = 402      # actuation → engine: command executed receipt
    ActuationState = 403    # actuation → engine: device state / sensor feedback
    ActuationFault = 404    # actuation → engine: device fault (triggers safety)
    SettleActuation = 405   # correlated mailbox cancellation and device quiescence
    ResumeActuation = 406   # enable new commands after a confirmed stop
    TrialCompleted = 501   # payload: TrialOutcome (generation + trial_number + Classification)
    EpochProcessed = 502   # local inspection only; never forwarded to output peers


class Event:
    """EventBus envelope containing an event type and its payload."""

    __slots__ = ("type", "data")

    def __init__(self, event_type: EventType, data: Any = None) -> None:
        self.type: EventType = event_type
        self.data: Any = data

    def __repr__(self) -> str:
        return f"Event({self.type!r}, data={type(self.data).__name__})"
