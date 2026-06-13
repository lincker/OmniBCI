"""
Regression — EventBus must survive crossing a spawn process boundary (B7).

On macOS/Windows, mp.Process.start() pickles the whole worker object.
SignalProcessingWorker holds the EventBus, whose _event_handlers dict contains
parent-side listeners (bound methods → repositories → threading.Lock,
asyncio loops, websockets) — none picklable. __getstate__ strips them: the
child only needs the queue to put_event().

Reproduces the field failure:
    TypeError: cannot pickle '_thread.lock' object
    ... when serializing dict item '_event_handlers' ...
"""
from __future__ import annotations

import pickle
import threading

from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.shared.events import EventType


class _RepoWithLock:
    """Stands in for InMemorySessionRepository (owns a threading.Lock)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def handler(self, event) -> None:  # pragma: no cover - identity only
        pass


class TestEventBusPickle:
    def test_pickles_with_unpicklable_handlers_registered(self):
        bus = EventBus()
        bus.register_event_listener(EventType.SessionStarted, _RepoWithLock().handler)
        data = pickle.dumps(bus)  # raised TypeError before the fix
        assert data

    def test_child_side_bus_has_no_handlers_and_is_inactive(self):
        bus = EventBus()
        bus.register_event_listener(EventType.TrialCompleted, _RepoWithLock().handler)
        clone = pickle.loads(pickle.dumps(bus))
        assert clone._event_handlers == {}
        assert clone._active is False
        assert clone._process_task is None

    def test_parent_bus_untouched_by_pickling(self):
        bus = EventBus()
        handler = _RepoWithLock().handler
        bus.register_event_listener(EventType.TrialCompleted, handler)
        pickle.dumps(bus)
        assert bus._event_handlers[EventType.TrialCompleted] == [handler]
