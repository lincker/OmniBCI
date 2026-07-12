#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-21 10:16
import asyncio
import inspect
import logging
import queue
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

    #: 阻塞消费的等待窗（秒）。stop() 后消费循环最迟一个窗口内退出；
    #: 事件到达时立即被取出（不再有 10ms 轮询粒度的派发延迟）。
    _GET_TIMEOUT_S = 0.2

    def _get_blocking(self) -> "Event | None":
        """在 executor 线程内阻塞取事件；等待窗内无事件返回 None。"""
        try:
            return self._event_queue.get(timeout=self._GET_TIMEOUT_S)
        except queue.Empty:
            return None

    async def _process_event(self) -> None:
        """事件消费循环（P2-7 事件化）。

        原实现为 ``empty() + 非阻塞 get + sleep(0.01)`` 忙轮询：空转时每秒
        100 次唤醒、事件平均带 5ms 派发延迟。现改为把 mp.Queue 的阻塞
        ``get(timeout)`` 放进默认 executor 线程执行：事件到达即刻唤醒
        （派发延迟 ≈ 0），空转时每秒仅 1/_GET_TIMEOUT_S 次唤醒。
        handler 派发逻辑不变（逐个隔离异常）。
        """
        logger.info('EventBus: processing started')
        loop = asyncio.get_running_loop()
        while self._active:
            if self._event_queue is None:
                await asyncio.sleep(0.01)
                continue

            event: "Event | None" = await loop.run_in_executor(None, self._get_blocking)
            if event is None:
                continue
            event_type = event.type
            if event_type in self._event_handlers:
                # 每个 handler 单独包裹：一个失败不影响其余（既有语义）。
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
        # 消费循环在下一个等待窗（≤_GET_TIMEOUT_S）内观察到 _active=False 退出。
        self._active = False
        self._process_task = None  # loop exits on _active=False; drop the ref

    def put_event(self, event: Event) -> None:
        if self._event_queue is None:
            logger.warning('EventBus.put_event called before queue is set; event dropped: %s', event)
            return
        self._event_queue.put_nowait(event)
