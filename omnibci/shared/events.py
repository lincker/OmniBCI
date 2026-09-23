#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
from enum import IntEnum
from typing import Any, Optional


class EventType(IntEnum):
    # acquisition
    ConnectDataProvider = 101
    DisconnectDataProvider = 102
    StartStream = 111
    StopStream = 112
    StopAndDrain = 113  # acquisition stop followed by an ordered processing barrier
    AcquisitionState = 121  # worker -> API: authoritative device-state snapshot
                            # payload: application.events.AcquisitionSnapshot
    CommandResult = 601      # command consumer -> API: application.events.CommandResult
    # Legacy numeric values are part of the versioned IPC wire contract.
    CloseAllNeuroEngineProcessSystem = 33
    # stimulation
    StartStimTrial = 311
    StimulationAck = 312   # stim → engine: ack/result of a stimulation command
    MessageFromStimulation = 9
    StartStimulation = 301
    StopStimulation = 302
    # actuation (external-device control; the actuation subsystem loads
    # an Effector plugin to carry out these commands)
    ActuationCommand = 401  # engine → actuation: drive the external device
    ActuationAck = 402      # actuation → engine: command executed receipt
    ActuationState = 403    # actuation → engine: device state / sensor feedback
    ActuationFault = 404    # actuation → engine: device fault (triggers safety)
    # --- domain events (carry rich domain objects as payload) ---------
    TrialCompleted = 501   # payload: TrialOutcome (session_id + trial_number + Classification)
    SessionStarted = 502   # payload: session_id str
    SessionEnded   = 503   # payload: SessionSummary (session_id + total_trials)
    # --- session lifecycle commands (API → engine → worker) -----------
    StartSession   = 504   # payload: {"session_id": str, "paradigm": str|None}
    EndSession     = 505   # payload: session_id str


class Event:
    """EventBus envelope containing an event type and its payload.

    Payload contracts:

      TrialCompleted   application.events.TrialOutcome
      SessionEnded     application.events.SessionSummary
      SessionStarted   str  (session_id)
      StartSession     dict {"session_id": str, "paradigm": str|None}
      EndSession       str  (session_id)
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
