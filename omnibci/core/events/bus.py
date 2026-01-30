#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
import asyncio
import logging
from .types import EventType, Event

logging.basicConfig(level=logging.DEBUG)


class EventBus:
    def __init__(self, event_queue=asyncio.Queue()):
        # events monitor status
        self._active = False
        self._event_handlers = {}
        self._event_queue = event_queue

    def set_event_queue(self, queue):
        self._event_queue = queue

    async def _process_event(self):
        logging.info('events processing')
        while self._active:
            if not self._event_queue.empty():
                event: Event = self._event_queue.get()
                event_type = event.type
                event_data = event.data
                if event_type in self._event_handlers:
                    try:
                        handlers = self._event_handlers[event_type]
                        # todo: use asyncio.gather to run all async handlers concurrently
                        for handler in handlers:
                            if asyncio.iscoroutinefunction(handler):
                                await handler(event)
                            else:
                                handler(event)
                    except Exception as e:
                        logging.error(f'Error processing events {event_type}: {e}')
            else:
                await asyncio.sleep(0.01)

    def register_event_listener(self, event_type: EventType, handler):
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)

    def unregister_event_listener(self, event_type: EventType, handler):
        if event_type not in self._event_handlers:
            return
        self._event_handlers[event_type].remove(handler)

    async def start(self):
        self._active = True
        asyncio.create_task(self._process_event())

    def stop(self):
        self._active = False

    def put_event(self, event: Event):
        self._event_queue.put_nowait(event)


