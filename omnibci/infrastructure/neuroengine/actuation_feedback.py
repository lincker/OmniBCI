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
        self.ack_count: int = 0
        self.fault_count: int = 0
        self.last_state: Optional[object] = None
        self.last_fault: Optional[object] = None

    def on_feedback(self, event: Event) -> None:
        """EventBus listener for ActuationAck / ActuationState / ActuationFault."""
        if event.type == EventType.ActuationAck:
            self.ack_count += 1
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
