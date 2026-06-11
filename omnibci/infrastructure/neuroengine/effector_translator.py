"""
EffectorCommandTranslator — ACL from decoded trials to effector commands.

Closed loop: each completed trial (TrialCompleted, payload = Classification)
becomes an EffectorCommand event on the bus, which the effector PeerChannel
routes out to the device controller.

This is the symmetric counterpart to how stimulation consumes TrialCompleted —
the engine emits domain events; an adapter translates them to a peer's wire
vocabulary. The engine domain never references the effector transport.

Mapping note: the command payload currently carries the decoded ``target_id``
(+ confidence/event_id). The target → concrete actuation mapping (which joint,
which direction, how far) is device-specific business logic and belongs on the
controller side or in a future configurable map — kept out of here on purpose.
"""
from __future__ import annotations

import logging

from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus

logger = logging.getLogger(__name__)


class EffectorCommandTranslator:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    def on_trial_completed(self, event: Event) -> None:
        """EventBus listener for TrialCompleted. Emits an EffectorCommand."""
        data = event.data
        # The bus payload is a TrialOutcome (session_id + trial_number +
        # classification) since the ACL enrichment — unwrap it. A bare
        # Classification (pre-enrichment shape) is still accepted for
        # backward compatibility, mirroring SessionAppService.on_trial_completed.
        clf = getattr(data, "classification", data)
        target_id = getattr(clf, "target_id", None)
        if target_id is None:
            logger.warning(
                "EffectorCommandTranslator: TrialCompleted payload %s has no "
                "target_id; no command emitted", type(data).__name__,
            )
            return
        command = {
            "target_id": int(target_id),
            "event_id": getattr(clf, "event_id", None),
            "confidence": getattr(clf, "confidence", None),
        }
        self._bus.put_event(Event(EventType.EffectorCommand, command))
        logger.info("EffectorCommand emitted: %s", command)
