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
    # neuro engine（历史遗留值 33/9：数值是 IPC wire 契约的一部分，
    # 重新编号需要 bump contracts.PROTOCOL_VERSION，故仅整理注释不改值）
    CloseAllNeuroEngineProcessSystem = 33
    # stimulation
    StartStimTrial = 311
    StimulationAck = 312   # stim → engine: ack/result of a stimulation command
    MessageFromStimulation = 9   # 历史遗留值，同上
    StartStimulation = 301
    StopStimulation = 302
    # effector (external-device control)
    EffectorCommand = 401  # engine → effector: drive the external device
    EffectorAck = 402      # effector → engine: command executed receipt
    EffectorState = 403    # effector → engine: device state / sensor feedback
    EffectorFault = 404    # effector → engine: device fault (triggers safety)
    # --- domain events (carry rich domain objects as payload) ---------
    TrialCompleted = 501   # payload: TrialOutcome (session_id + trial_number + Classification)
    SessionStarted = 502   # payload: session_id str
    SessionEnded   = 503   # payload: SessionSummary (session_id + total_trials)
    # --- session lifecycle commands (API → engine → worker) -----------
    StartSession   = 504   # payload: {"session_id": str, "paradigm": str|None}
    EndSession     = 505   # payload: session_id str


class Event:
    """EventBus 信封：type 标识事件种类，data 携带载荷。

    载荷契约（P2-8，权威表）——各 EventType 对应的 data 类型：

      TrialCompleted   application.events.TrialOutcome
      SessionEnded     application.events.SessionSummary
      SessionStarted   str  (session_id)
      StartSession     dict {"session_id": str, "paradigm": str|None}
      EndSession       str  (session_id)
      StartStimulation str|None (paradigm)
      StartStimTrial   dict {"n_trials": int, "trial_params": list|None}
      StimulationAck   dict (make_stimulation_ack 构造，跨 socket 为 JSON)
      Effector*        dict (设备命令/反馈，跨 socket 为 JSON)
      其余命令类事件    None

    消费方对富载荷做显式 isinstance 检查；旧的 bare-Classification /
    bare-int 兼容形状已于 P2-8 移除。
    """

    __slots__ = ("type", "data")

    def __init__(self, event_type: Optional[EventType] = None, data: Any = None) -> None:
        self.type: Optional[EventType] = event_type
        self.data: Any = data

    def __repr__(self) -> str:
        return f"Event({self.type!r}, data={type(self.data).__name__})"

    # NOTE: create_from_json was removed — it called cls(**event_json) with key
    # 'type' against parameter 'event_type' (TypeError on any use), and wire
    # decoding is owned by infrastructure/ipc/contracts.body_to_event.
    # NOTE: to_json was removed (P2-8) — dead code; wire encoding is owned by
    # infrastructure/ipc/contracts.event_to_body.

