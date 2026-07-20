"""
ActuationCommandTranslator — ACL from decoded trials to actuation commands.

Closed loop: each completed trial (TrialCompleted, payload = Classification)
becomes an ActuationCommand event on the bus, which the actuation PeerChannel
routes out to the device controller.

This is the symmetric counterpart to how stimulation consumes TrialCompleted —
the engine emits domain events; an adapter translates them to a peer's wire
vocabulary. The engine domain never references the actuation transport.

Mapping note: the command payload currently carries the decoded ``target_id``
(+ confidence/event_id). The target → concrete actuation mapping (which joint,
which direction, how far) is device-specific business logic and belongs on the
controller side or in a future configurable map — kept out of here on purpose.
"""

import logging
import time

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus

logger = logging.getLogger(__name__)


class ActuationCommandTranslator:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus
        # Monotonic per engine process — enough for correlation/accounting
        # (replies cannot outlive a connection, so restarts starting over at 1
        # are harmless). Echoed back by the actuation side in ack/fault.
        self._seq = 0

    def on_trial_completed(self, event: Event) -> None:
        """EventBus listener for TrialCompleted. Emits an ActuationCommand."""
        data = event.data
        # The bus payload is a TrialOutcome (session_id + trial_number +
        # classification) since the ACL enrichment — unwrap it. A bare
        # Classification (pre-enrichment shape) is still accepted for
        # backward compatibility, mirroring SessionAppService.on_trial_completed.
        clf = getattr(data, "classification", data)
        target_id = getattr(clf, "target_id", None)
        if target_id is None:
            logger.warning(
                "ActuationCommandTranslator: TrialCompleted payload %s has no "
                "target_id; no command emitted", type(data).__name__,
            )
            return
        self._seq += 1
        command = {
            "seq": self._seq,              # correlation id, echoed in ack/fault
            "issued_at": time.time(),      # engine clock; advisory age marker
            "target_id": int(target_id),
            "event_id": getattr(clf, "event_id", None),
            "confidence": getattr(clf, "confidence", None),
        }
        self._bus.put_event(Event(EventType.ActuationCommand, command))
        logger.info("ActuationCommand emitted: %s", command)
