#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-12-11 10:34

import asyncio
import logging
from typing import Optional

from core.data.types import SignalChunk
from omnibci.core.events.types import Event, EventType

class ProcessingState:
    """Base class for processing states."""

    def __init__(self, context):
        self.context = context

    async def run(self):
        """Execute state logic. Must be implemented by subclasses."""
        raise NotImplementedError



class WaitingForEpochState(ProcessingState):

    async def run(self):
        # Wait if no data available
        if self.context.data_manager.empty():
            await asyncio.sleep(0.01)
            return

        chunk: SignalChunk = self.context.data_manager.get_data()
        start_idx, event_id = self.context.epoch_manager.detect_start(chunk)

        if start_idx >= 0:
            logging.info(f"Start events detected: index={start_idx}, event_id={event_id}")
            self.context.epoch_manager.initialize_epoch(chunk, event_id)
            self.context.transition_to_collecting()


class CollectingEpochState(ProcessingState):
    """
    Processing state - analyzing epoch data.
    """

    async def run(self):
        # Wait if no data available
        if self.context.data_manager.empty():
            await asyncio.sleep(0.01)
            return

        chunk = self.context.data_manager.get_data()

        # Check for events
        end_idx, _ = self.context.epoch_manager.detect_end(chunk)
        start_idx, new_event_id = self.context.epoch_manager.detect_start(chunk)

        has_end_event = end_idx >= 0
        has_start_event = start_idx >= 0

        # Case 1: Normal data (no events) - append and analyze
        if not has_end_event and not has_start_event:
            self._process_normal_data(chunk)
            return

        # Case 2: End events detected
        if has_end_event:
            self._process_end_event(chunk, end_idx, has_start_event, start_idx, new_event_id)
            return

        # Case 3: Start events only (shouldn't happen in normal flow, but handle it)
        if has_start_event:
            self._process_unexpected_start(chunk, start_idx, new_event_id)

    def _process_normal_data(self, chunk):
        """Process chunk with no events."""
        self.context.epoch_manager.append_chunk(chunk)
        is_complete, result = self.context.epoch_manager.analyse(force_recognize=False)

        if is_complete:
            self._emit_result(result)
            self.context.transition_to_waiting()

    def _process_end_event(self, chunk, end_idx: int, has_start_event: bool,
                           start_idx: int, new_event_id: Optional[int]):
        """Process chunk containing end events."""
        # Append data up to end events and force recognition
        self.context.epoch_manager.append_until_index(chunk, end_idx)
        _, result = self.context.epoch_manager.analyse(force_recognize=True)
        logging.info(f"End events detected: forced recognition, result={result}")
        self._emit_result(result)

        # Check if new epoch starts in same chunk
        if has_start_event:
            logging.info(f"New epoch begins in same chunk: index={start_idx}")
            self.context.epoch_manager.initialize_epoch(chunk, new_event_id)
            # Stay in same state
        else:
            self.context.transition_to_waiting()

    def _process_unexpected_start(self, chunk, start_idx: int, event_id: int):
        """Handle unexpected start events during processing."""
        logging.warning("Unexpected start events detected during processing")

        # Append data before the new start events and force recognition
        self.context.epoch_manager.append_until_index(chunk, start_idx)
        _, result = self.context.epoch_manager.analyse(force_recognize=True)

        logging.info(f"Forced recognition due to new start: result={result}")
        self._emit_result(result)

        # Initialize new epoch
        self.context.epoch_manager.initialize_epoch(chunk, event_id)

    def _emit_result(self, result):
        """Emit recognition result as an events."""
        logging.info(f"Recognition result: {result}")
        event = Event(EventType.RecognizedResult, result)
        self.context.event_bus.put_event(event)
