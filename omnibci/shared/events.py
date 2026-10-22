#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum, unique
from typing import Any, Optional


@unique
class EventType(IntEnum):
    # acquisition
    ConnectDataProvider = 101
    DisconnectDataProvider = 102
    GetSourceCheckpoint = 105
    RestoreSourceCheckpoint = 106
    UpdateAcquisitionRevision = 107
    GetSourceMetadata = 108
    StartStream = 111
    StopStream = 112
    StopAndDrain = 113  # acquisition stop followed by an ordered processing barrier
    StartAcquisitionPreview = 114
    StopAcquisitionPreview = 115
    GetAcquisitionPreview = 116
    StageProcessingConfiguration = 211  # stage a pipeline until the next epoch boundary
    CancelProcessingConfiguration = 212  # reconcile or cancel a staged pipeline
    UpdateProcessingContext = 213  # acknowledge output metadata after an ordered drain
    PrepareSignalProcessing = 214
    AcquisitionState = 121  # worker -> API: authoritative device-state snapshot
                            # payload: application.events.AcquisitionSnapshot
    CommandResult = 601      # command consumer -> API: application.events.CommandResult
    PeerHello = 701          # external peer -> engine: pinned runtime identity
    PeerAccepted = 702       # engine -> external peer: matching identity accepted
    # Legacy numeric values are part of the versioned IPC wire contract.
    CloseAllNeuroEngineProcessSystem = 33
    # stimulation
    StartStimTrial = 311
    StimulationAck = 312   # stim → engine: ack/result of a stimulation command
    MessageFromStimulation = 9
    StartStimulation = 301
    StopStimulation = 302
    PauseStimulation = 303  # correlated renderer stop and trigger clear
    # actuation (external-device control; the actuation subsystem loads
    # an Effector plugin to carry out these commands)
    ActuationCommand = 401  # engine → actuation: drive the external device
    ActuationAck = 402      # actuation → engine: command executed receipt
    ActuationState = 403    # actuation → engine: device state / sensor feedback
    ActuationFault = 404    # actuation → engine: device fault (triggers safety)
    SettleActuation = 405   # correlated mailbox cancellation and device quiescence
    TrialCompleted = 501   # payload: TrialOutcome (generation + trial_number + Classification)


class Event:
    """EventBus envelope containing an event type and its payload.

    Payload contracts:

      TrialCompleted   application.events.TrialOutcome
      StartStimulation str|None (paradigm)
      StartStimTrial   dict {"n_trials": int, "trial_params": list|None}
      AcquisitionState application.events.AcquisitionSnapshot
      CommandResult    application.events.CommandResult
      StimulationAck   dict produced by make_stimulation_ack
      Actuation*       dict containing device commands or feedback
      Other commands   None
    """

    __slots__ = ("type", "data")

    def __init__(self, event_type: Optional[EventType] = None, data: Any = None) -> None:
        self.type: Optional[EventType] = event_type
        self.data: Any = data

    def __repr__(self) -> str:
        return f"Event({self.type!r}, data={type(self.data).__name__})"
