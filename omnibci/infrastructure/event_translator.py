"""
DomainEventPublisher — Anti-Corruption Layer (ACL).

职责：将领域层的富事件对象（domain/events.py）翻译成基础设施 EventBus
所使用的轻量 Event(EventType, data) 并投递。

为什么需要这一层
-----------------
领域层（domain/）与基础设施 IPC 层（core/events/）各自有独立的事件表示：

  domain/events.py       → 富值对象（TrialCompleted、SessionStarted…）
  core/events/types.py   → 轻量 IPC 信封（Event + EventType IntEnum）

两者不能合并：领域层不应依赖 mp.Queue / EventType 这类基础设施概念；
IPC 信封需要跨进程序列化，不适合携带复杂领域对象（除 payload 以外）。

DomainEventPublisher 是唯一知道"如何翻译"的地方，消除了原来散落在
processor.py / state.py 中的自由函数 _publish_domain_event 及
state.py 里的延迟循环 import。

依赖方向
---------
infrastructure.event_translator
    → domain.events          (知道哪些领域事件存在)
    → core.events.types      (知道基础设施 EventType)
    → core.events.bus        (仅用于类型注解)

processor.py 和 state.py 只依赖本模块，不再互相依赖。
"""
from __future__ import annotations

import logging
from typing import Iterable, TYPE_CHECKING

from omnibci.domain.events import DomainEvent, SessionEnded, SessionStarted, TrialCompleted
from omnibci.shared.events import Event, EventType
from omnibci.infrastructure.outbound import TrialOutcome, SessionSummary

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
                ),
            )

        if isinstance(domain_event, SessionStarted):
            return Event(EventType.SessionStarted, domain_event.session_id)

        logger.warning(
            "DomainEventPublisher: no translation for %s — event dropped",
            type(domain_event).__name__,
        )
        return None
