"""
DomainEventPublisher — Anti-Corruption Layer (ACL).

Translates rich domain events into lightweight Event(EventType, data)
envelopes for the infrastructure EventBus. The domain stays independent of
IPC transports, while pickle-safe application payloads cross process boundaries.

This module owns the translation table. Signal Processing states and their processor
share this publisher without importing one another.
"""

import logging
from typing import Iterable, TYPE_CHECKING

from omnibci.domain.events import DomainEvent, SessionEnded, SessionStarted, TrialCompleted
from omnibci.shared.events import Event, EventType
from omnibci.application.events import SessionSummary, TrialOutcome

if TYPE_CHECKING:
    from omnibci.infrastructure.ipc.event_bus import EventBus

logger = logging.getLogger(__name__)


class DomainEventPublisher:
    """
    Translates domain events to infrastructure IPC events and puts them
    on the shared EventBus.

    Usage (inside SignalProcessor)::

        self.event_publisher = DomainEventPublisher(event_bus)
        ...
        self.event_publisher.publish_all(session.pop_events())
    """

    def __init__(self, event_bus: "EventBus") -> None:
        self._bus = event_bus

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def publish(self, domain_event: DomainEvent) -> None:
        """Translate one domain event and put it on the EventBus."""
        infra_event = self._translate(domain_event)
        if infra_event is not None:
            self._bus.put_event(infra_event)

    def publish_all(self, domain_events: Iterable[DomainEvent]) -> None:
        """Convenience: translate and publish an iterable of domain events."""
        for ev in domain_events:
            self.publish(ev)

    # ------------------------------------------------------------------
    # Translation table (single place to extend)
    # ------------------------------------------------------------------

    def _translate(self, domain_event: DomainEvent) -> Event | None:
        """
        Map a DomainEvent subclass to the corresponding infrastructure Event.

        Returns None (and logs a warning) for unrecognised event types so
        that unknown events are silently skipped rather than crashing the
        signal-processing loop.
        """
        if isinstance(domain_event, TrialCompleted):
            # Carry session context + Classification so downstream listeners
            # (WebSocket broadcaster, SessionAppService) have session_id and
            # trial_number — not just the bare Classification.
            return Event(
                EventType.TrialCompleted,
                TrialOutcome(
                    session_id=domain_event.session_id,
                    trial_number=domain_event.trial_number,
                    classification=domain_event.classification,
                    config_revision=domain_event.config_revision,
                ),
            )

        if isinstance(domain_event, SessionEnded):
            # Carry session_id alongside total_trials so the WS SessionEnded
            # message can report which session ended.
            return Event(
                EventType.SessionEnded,
                SessionSummary(
                    session_id=domain_event.session_id,
                    total_trials=domain_event.total_trials,
                    config_revision=domain_event.config_revision,
                    interruption_reason=domain_event.interruption_reason,
                ),
            )

        if isinstance(domain_event, SessionStarted):
            return Event(EventType.SessionStarted, domain_event.session_id)

        logger.warning(
            "DomainEventPublisher: no translation for %s — event dropped",
            type(domain_event).__name__,
        )
        return None
