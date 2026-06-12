"""
EffectorFeedbackHandler — engine-side consumer of effector → engine inbound.

Mirrors StimulationFeedbackHandler. Consumes the three effector inbound event
types routed in by the effector PeerChannel:

  - EffectorAck   : a command was executed (receipt).
  - EffectorState : device state / sensor feedback (closed-loop input).
  - EffectorFault : device fault — surfaced as a warning; the seam where Phase 4
                    safety logic (failsafe / safe-stop) will hook in.

Side-effect-light by design: it records observability state and logs. It does
NOT yet command a safe state on fault — that is wired with the connection state
machine in Phase 4.
"""

import logging
from typing import Optional

from omnibci.shared.events import Event, EventType

logger = logging.getLogger(__name__)


class EffectorFeedbackHandler:
    def __init__(self) -> None:
        self.ack_count: int = 0
        self.fault_count: int = 0
        self.last_state: Optional[object] = None
        self.last_fault: Optional[object] = None

    def on_feedback(self, event: Event) -> None:
        """EventBus listener for EffectorAck / EffectorState / EffectorFault."""
        if event.type == EventType.EffectorAck:
            self.ack_count += 1
            logger.info("Effector ack: %s", event.data)
        elif event.type == EventType.EffectorState:
            self.last_state = event.data
            logger.debug("Effector state: %s", event.data)
        elif event.type == EventType.EffectorFault:
            self.fault_count += 1
            self.last_fault = event.data
            logger.warning("Effector fault: %s", event.data)
        else:  # pragma: no cover - defensive; only the 3 types are registered
            logger.debug("EffectorFeedbackHandler: ignoring %s", event.type)
