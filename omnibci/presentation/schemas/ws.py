"""
WebSocket message schemas.

These Pydantic models define the JSON structure pushed to connected clients.
Every message has a top-level `event` discriminator so clients can route
messages without inspecting payload shape.
"""

from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


class TrialCompletedMessage(BaseModel):
    """
    Sent when a BCI trial finishes and a Classification is available.

    Maps 1-to-1 to the domain TrialCompleted event; heavy signal data
    (the Epoch) is intentionally excluded — clients receive the result,
    not the raw EEG.
    """
    event: Literal["TrialCompleted"] = "TrialCompleted"
    session_id: str
    trial_number: int
    config_revision: Optional[str] = None
    target_id: int = Field(..., description="Index of the recognised stimulus target")
    event_id: int = Field(..., description="Trigger event that opened the epoch")
    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="Decoder confidence in [0, 1]; None if unavailable"
    )
    forced: bool = Field(
        False,
        description="True when recognition was triggered by an end/reset event"
    )
    produced_at: datetime = Field(..., description="UTC timestamp of classification")


class SessionStartedMessage(BaseModel):
    event: Literal["SessionStarted"] = "SessionStarted"
    session_id: str


class SessionEndedMessage(BaseModel):
    event: Literal["SessionEnded"] = "SessionEnded"
    # session_id is not yet propagated through the SessionEnded EventBus payload
    # (it carries total_trials); kept optional until the ACL is enriched.
    session_id: Optional[str] = None
    total_trials: int


class StimulationAckMessage(BaseModel):
    """
    Acknowledgement of a stimulation command, originated by the stimulation
    process and relayed to clients: tells the operator whether a start/stop/trial
    command was actually carried out by the OpenGL renderer.
    """
    event: Literal["StimulationAck"] = "StimulationAck"
    command: str = Field(..., description="The acknowledged command, e.g. 'StartStimTrial'")
    target_index: Optional[int] = Field(None, description="Target index for trial commands")
    status: Literal["ok", "error"] = Field(..., description="Outcome on the stimulation side")
    message: Optional[str] = Field(None, description="Human-readable detail (e.g. error reason)")


class PingMessage(BaseModel):
    """
    Server heartbeat (every ~15 s; see websockets/results.py).

    Carries no payload — its only job is liveness: the browser cannot observe
    protocol-level ping/pong, so clients use *any* message (including this one)
    to reset their staleness timer and force a reconnect when the feed goes
    silent.  Sending it also lets the server reap dead connections during idle
    periods instead of waiting for the next real broadcast.
    """
    event: Literal["Ping"] = "Ping"


# Union type for documentation / client code generation
WsMessage = Union[
    TrialCompletedMessage,
    SessionStartedMessage,
    SessionEndedMessage,
    StimulationAckMessage,
    PingMessage,
]
