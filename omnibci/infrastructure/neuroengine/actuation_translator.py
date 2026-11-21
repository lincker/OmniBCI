"""
ActuationCommandTranslator — ACL from decoded trials to actuation commands.

Closed loop: each completed trial (TrialCompleted, payload = TrialOutcome)
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
from uuid import uuid4

from omnibci.application.commands.actuation_commands import ActuationIntent
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus

logger = logging.getLogger(__name__)


class ActuationCommandTranslator:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    def on_trial_completed(self, event: Event) -> None:
        """EventBus listener for TrialCompleted. Emits an ActuationCommand."""
        data = event.data
        if not isinstance(data, TrialOutcome) or not isinstance(data.classification, Classification):
            logger.warning("ActuationCommandTranslator requires a canonical TrialOutcome")
            return
        clf = data.classification
        try:
            command = ActuationIntent(
                command_id=uuid4().hex, issued_at=time.time(),
                origin={"generation": data.generation, "trial_number": data.trial_number,
                        "processing_revision": data.processing_revision,
                        "acquisition_revision": data.acquisition_revision,
                        "forced": clf.forced, "produced_at": clf.produced_at.isoformat()},
                parameters={"target_id": clf.target_id, "event_id": clf.event_id, "confidence": clf.confidence},
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Invalid actuation trial origin: %s", exc)
            return
        self._bus.put_event(Event(EventType.ActuationCommand, command))
        logger.info("ActuationCommand emitted: %s", command)
