#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
import asyncio
import inspect
import logging
from typing import Optional
from omnibci.shared.events import EventType, Event

logger = logging.getLogger(__name__)


class EventBus:
    # Bug 1 fixed: removed mutable default argument `event_queue=asyncio.Queue()`.
    # A default asyncio.Queue() is created once at class definition time and
    # shared by every instance, causing all EventBus objects to read from and
    # write to the same queue.  Default is now None; callers must call
    # set_event_queue() or pass a queue explicitly.
    def __init__(self, event_queue=None):
        self._active = False
        self._event_handlers = {}
        self._event_queue: Optional[asyncio.Queue] = event_queue
        # Strong reference to the consumer task — asyncio only keeps weak
        # references to tasks, so without this the processing loop could be
        # garbage-collected mid-run (same fix as ListenTransport._serve_task).
        self._process_task: Optional[asyncio.Task] = None

    def set_event_queue(self, queue) -> None:
        self._event_queue = queue

    def __getstate__(self):
        """
        Strip process-local state when crossing a process boundary (spawn
        platforms — macOS/Windows — pickle the whole worker object on start).

        Handlers are bound methods reaching into repositories (threading.Lock),
        asyncio loops and websockets: none of it picklable, none of it
        meaningful in the child. The child only needs the mp.Queue to
        put_event(); the parent keeps the one bus instance that dispatches to
        handlers. Without this, SignalProcessingWorker.start() dies with
        "TypeError: cannot pickle '_thread.lock' object" on macOS.
        """
        state = self.__dict__.copy()
        state['_event_handlers'] = {}
        state['_process_task'] = None
        state['_active'] = False
        return state

    async def _process_event(self) -> None:
        logger.info('EventBus: processing started')
        while self._active:
            if self._event_queue is None:
                await asyncio.sleep(0.01)
                continue

            if not self._event_queue.empty():
                # Bug 2 fixed: was `self._event_queue.get()` (sync, blocks the
                # event loop when the queue is an asyncio.Queue).  The queue is
                # set to an mp.Queue at runtime via set_event_queue(), so the
                # correct call is the non-blocking `.get()` guarded by
                # `.empty()` — which is what we have here and is correct for
                # mp.Queue.  Added an explicit comment so the intent is clear.
                # (For an asyncio.Queue the correct call would be
                # `await queue.get()`; the current mp.Queue usage is correct.)
                event: Event = self._event_queue.get()  # mp.Queue: non-blocking after empty() check
                event_type = event.type
                if event_type in self._event_handlers:
                    # Bug 3 fixed: try/except was wrapping the entire handler
                    # loop, so a single failing handler would abort all
                    # subsequent handlers for the same event type.  Each
                    # handler is now wrapped individually so one failure is
                    # isolated and logged without affecting the others.
                    for handler in list(self._event_handlers[event_type]):
                        try:
                            if inspect.iscoroutinefunction(handler):
                                await handler(event)
                            else:
                                handler(event)
                        except Exception as exc:
                            logger.error(
                                'EventBus: handler %s raised for event %s: %s',
                                getattr(handler, '__name__', repr(handler)),
                                event_type,
                                exc,
                                exc_info=True,
                            )
            else:
                await asyncio.sleep(0.01)
        logger.info('EventBus: processing stopped')

    def register_event_listener(self, event_type: EventType, handler) -> None:
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def unregister_event_listener(self, event_type: EventType, handler) -> None:
        if event_type not in self._event_handlers:
            return
        try:
            self._event_handlers[event_type].remove(handler)
        except ValueError:
            pass  # handler was already removed; ignore

    async def start(self) -> None:
        self._active = True
        self._process_task = asyncio.create_task(self._process_event())

    def stop(self) -> None:
        self._active = False
        self._process_task = None  # loop exits on _active=False; drop the ref

    def put_event(self, event: Event) -> None:
        if self._event_queue is None:
            logger.warning('EventBus.put_event called before queue is set; event dropped: %s', event)
            return
        self._event_queue.put_nowait(event)
