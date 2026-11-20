"""
ActuationFeedbackHandler — engine-side consumer of actuation → engine inbound.

Mirrors StimulationFeedbackHandler. Consumes the three actuation inbound event
types routed in by the actuation PeerChannel:

  - ActuationAck   : a command was executed (receipt).
  - ActuationState : device state / sensor feedback (closed-loop input).
  - ActuationFault : device fault — surfaced as a warning; the seam where Phase 4
                    safety logic (failsafe / safe-stop) will hook in.

Side-effect-light by design: it records observability state and logs. It does
NOT yet command a safe state on fault — that is wired with the connection state
machine in Phase 4.
"""

import logging
from typing import Optional

from omnibci.shared.events import Event, EventType

logger = logging.getLogger(__name__)


class ActuationFeedbackHandler:
    def __init__(self) -> None:
        self.ack_count: int = 0          # total acks, any status
        self.ok_count: int = 0
        self.superseded_count: int = 0
        self.stale_count: int = 0
        self.fault_count: int = 0
        self.last_acked_command_id: Optional[str] = None
        self.last_state: Optional[object] = None
        self.last_fault: Optional[object] = None

    def on_feedback(self, event: Event) -> None:
        """EventBus listener for ActuationAck / ActuationState / ActuationFault."""
        if event.type == EventType.ActuationAck:
            data = event.data if isinstance(event.data, dict) else {}
            status = data.get("status")
            identity = data.get("command_id")
            if (not isinstance(status, str) or status not in {"ok", "superseded", "stale", "cancelled"}
                    or not isinstance(identity, str) or not identity or identity != identity.strip()):
                logger.warning("Ignoring actuation acknowledgement without a valid status and command identity")
                return
            self.ack_count += 1
            if status == "ok":
                self.ok_count += 1
            elif status == "superseded":
                self.superseded_count += 1
            elif status == "stale":
                self.stale_count += 1
            self.last_acked_command_id = identity
            logger.info("Actuation ack: %s", event.data)
        elif event.type == EventType.ActuationState:
            self.last_state = event.data
            logger.debug("Actuation state: %s", event.data)
        elif event.type == EventType.ActuationFault:
            self.fault_count += 1
            self.last_fault = event.data
            logger.warning("Actuation fault: %s", event.data)
        else:  # pragma: no cover - defensive; only the 3 types are registered
            logger.debug("ActuationFeedbackHandler: ignoring %s", event.type)
