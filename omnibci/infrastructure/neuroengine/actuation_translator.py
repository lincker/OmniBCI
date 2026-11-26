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
from omnibci.application.commands.actuation_commands import intent_from_trial
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.ipc.event_bus import EventBus

logger = logging.getLogger(__name__)


class ActuationCommandTranslator:
    def __init__(self, event_bus: EventBus) -> None:
        self._bus = event_bus

    def on_trial_completed(self, event: Event) -> None:
        try:
            command = intent_from_trial(event.data)
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid actuation trial origin: %s", exc)
            return
        self._bus.put_event(Event(EventType.ActuationCommand, command))
