"""
StimulationFeedbackHandler — engine-side consumer of the stimulation → engine
inbound path (Phase 2 of the comms refactor).

Until now the inbound channel was full-duplex at the transport level, but the
only consumer of ``StimulationAck`` was the presentation layer (WebSocket
broadcast). The engine itself ignored stimulation feedback. This handler makes
the engine an actual consumer:

  - records presentation outcomes (ok / error counts, last ack);
  - surfaces failures (an ``error`` ack means the stimulus never showed, so any
    epoch opened for that trigger is suspect) via a warning log.

It is deliberately side-effect-light and is the seam for richer domain reactions
later (e.g. invalidating the trial whose stimulus failed, or gating acquisition
on a readiness ack) — those are business decisions, kept out until specified.

The ack payload shape (see ``infrastructure.outbound.make_stimulation_ack``):
    {"command": str, "status": "ok"|"error", "target_index": int|None,
     "message": str|None}
"""

import logging
from typing import Optional

from omnibci.shared.events import Event

logger = logging.getLogger(__name__)


class StimulationFeedbackHandler:
    """Consumes StimulationAck events on the engine EventBus."""

    def __init__(self) -> None:
        self.ok_count: int = 0
        self.error_count: int = 0
        self.last_ack: Optional[dict] = None

    def on_ack(self, event: Event) -> None:
        """EventBus listener for EventType.StimulationAck. Never raises."""
        data = event.data if isinstance(event.data, dict) else {}
        command = str(data.get("command", ""))
        status = data.get("status", "ok")
        target_index = data.get("target_index")
        message = data.get("message")
        self.last_ack = data

        if status == "error":
            self.error_count += 1
            logger.warning(
                "Stimulation failed to present %s (target=%s): %s",
                command, target_index, message,
            )
        else:
            self.ok_count += 1
            logger.info(
                "Stimulation presented %s (target=%s)", command, target_index
            )
