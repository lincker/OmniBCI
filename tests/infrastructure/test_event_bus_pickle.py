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
        # Lifecycle locks are process/loop-local just like consumer tasks.
        bus._lifecycle_lock = asyncio.Lock()
        clone = pickle.loads(pickle.dumps(bus))
        assert clone._event_handlers == {}
        assert clone._active is False
        assert clone._process_task is None
        assert clone._lifecycle_lock is None

    def test_parent_bus_untouched_by_pickling(self):
        bus = EventBus()
        handler = _RepoWithLock().handler
        bus.register_event_listener(EventType.TrialCompleted, handler)
        pickle.dumps(bus)
        assert bus._event_handlers[EventType.TrialCompleted] == [handler]


# ---------------------------------------------------------------------------
# P2-7: 事件化消费循环（阻塞 get 取代 empty()+sleep 轮询）
# ---------------------------------------------------------------------------

import asyncio
import queue as _queue
import time

import pytest

from omnibci.shared.events import Event, EventType


class TestEventDrivenConsumption:
    @pytest.mark.asyncio
    async def test_event_dispatched_promptly(self):
        """事件放入后应立即派发（无 10ms 轮询粒度延迟）。"""
        bus = EventBus()
        bus.set_event_queue(_queue.Queue())   # 与 mp.Queue 同 get(timeout) 协议
        got = asyncio.get_running_loop().create_future()
        bus.register_event_listener(
            EventType.SessionStarted, lambda ev: got.set_result(ev.data))
        await bus.start()
        try:
            t0 = time.perf_counter()
            bus.put_event(Event(EventType.SessionStarted, "s1"))
            data = await asyncio.wait_for(got, timeout=1.0)
            elapsed = time.perf_counter() - t0
            assert data == "s1"
            assert elapsed < 0.1   # 远小于旧实现的轮询粒度上界
        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_stop_terminates_consumer_within_wait_window(self):
        bus = EventBus()
        bus.set_event_queue(_queue.Queue())
        await bus.start()
        task = bus._process_task
        await asyncio.wait_for(bus.stop(), timeout=bus._GET_TIMEOUT_S + 0.5)
        assert task.done()
        assert bus._process_task is None

    @pytest.mark.asyncio
    async def test_handler_error_does_not_stop_consumption(self):
        bus = EventBus()
        bus.set_event_queue(_queue.Queue())
        got = asyncio.get_running_loop().create_future()
        bus.register_event_listener(
            EventType.SessionStarted, lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.register_event_listener(
            EventType.SessionStarted, lambda ev: got.set_result(True))
        await bus.start()
        try:
            bus.put_event(Event(EventType.SessionStarted, "x"))
            assert await asyncio.wait_for(got, timeout=1.0)
        finally:
            await bus.stop()


class _BlockingQueue:
    """Queue double that lets a test stop the bus while get() is in flight."""

    def __init__(self, item=None) -> None:
        self.item = item
        self.get_entered = threading.Event()
        self.release_get = threading.Event()
        self.close_calls = 0

    def get(self, timeout=None):
        self.get_entered.set()
        self.release_get.wait(timeout=timeout)
        if not self.release_get.is_set():
            raise _queue.Empty
        return self.item

    def put_nowait(self, item) -> None:
        self.item = item
        self.release_get.set()

    def close(self) -> None:
        self.close_calls += 1


class TestEventBusLifecycle:
    @pytest.mark.asyncio
    async def test_concurrent_start_creates_one_consumer(self):
        bus = EventBus(_queue.Queue())

        await asyncio.gather(bus.start(), bus.start(), bus.start())
        task = bus._process_task
        assert task is not None

        await bus.start()
        assert bus._process_task is task
        await bus.stop()

    @pytest.mark.asyncio
    async def test_concurrent_stop_awaits_and_clears_one_consumer(self):
        bus = EventBus(_queue.Queue())
        await bus.start()
        task = bus._process_task

        await asyncio.gather(bus.stop(), bus.stop(), bus.stop())

        assert task is not None and task.done()
        assert bus._process_task is None
        assert bus._active is False

    @pytest.mark.asyncio
    async def test_restart_uses_a_new_consumer_after_old_one_exits(self):
        bus = EventBus(_queue.Queue())
        await bus.start()
        first = bus._process_task
        await bus.stop()
        assert first is not None and first.done()

        await bus.start()
        second = bus._process_task
        assert second is not None and second is not first
        assert first.done() and not second.done()
        await bus.stop()

    @pytest.mark.asyncio
    async def test_item_returned_after_stop_is_not_dispatched(self):
        event = Event(EventType.SessionStarted, "too-late")
        queue = _BlockingQueue(event)
        bus = EventBus(queue)
        received = []
        bus.register_event_listener(EventType.SessionStarted, received.append)
        await bus.start()

        entered = await asyncio.to_thread(queue.get_entered.wait, 1.0)
        assert entered
        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)  # let stop() clear _active before get() returns
        assert bus._active is False
        queue.release_get.set()
        await stop_task

        assert received == []

    @pytest.mark.asyncio
    async def test_stop_is_idempotent_and_does_not_close_injected_queue(self):
        queue = _BlockingQueue()
        bus = EventBus(queue)
        await bus.start()

        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)
        queue.release_get.set()
        await stop_task
        await bus.stop()

        assert queue.close_calls == 0

    @pytest.mark.asyncio
    async def test_cancelled_stop_finishes_unique_consumer_before_propagating(self):
        queue = _BlockingQueue()
        bus = EventBus(queue)
        await bus.start()
        consumer = bus._process_task
        assert await asyncio.to_thread(queue.get_entered.wait, 1.0)

        stop_task = asyncio.create_task(bus.stop())
        await asyncio.sleep(0)
        stop_task.cancel()
        await asyncio.sleep(0)

        assert consumer is not None and not consumer.done()
        assert not stop_task.done()
        queue.release_get.set()

        with pytest.raises(asyncio.CancelledError):
            await stop_task
        assert consumer.done()
        assert bus._process_task is None
        assert bus._active is False
