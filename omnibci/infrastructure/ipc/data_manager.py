#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-04-09 10:41
import logging
import queue
from dataclasses import dataclass
from time import monotonic_ns

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _QueuedData:
    data: object
    enqueued_at_ns: int


class DataManager:
    def __init__(self, buffer):
        self.buffer = buffer

    def put_data(self, data):
        self.buffer.put_nowait(_QueuedData(data, monotonic_ns()))

    def get_data(self, timeout=None):
        """Return the next payload, or None when the optional timeout expires.

        Instrumented messages carry a same-host monotonic enqueue timestamp.
        Raw messages inserted directly into the queue have no timing metadata.
        """
        try:
            item = self.buffer.get() if timeout is None else self.buffer.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, _QueuedData):
            elapsed_ms = (monotonic_ns() - item.enqueued_at_ns) / 1_000_000
            data = item.data
            logger.debug(
                "Data queue: item_type=%s sequence_id=%s command_id=%s queue_dwell_ms=%.3f",
                type(data).__name__, getattr(data, "sequence_id", None),
                getattr(data, "command_id", None), elapsed_ms,
                extra={
                    "queue_dwell_ms": elapsed_ms,
                    "item_type": type(data).__name__,
                    "sequence_id": getattr(data, "sequence_id", None),
                    "command_id": getattr(data, "command_id", None),
                },
            )
            return data
        return item

    def empty(self):
        return self.buffer.empty()
