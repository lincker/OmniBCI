#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 09:45

import logging
import numpy as np
from typing import Optional, Tuple

from omnibci.interfaces.algorithm import DecoderBase, EpochDetectorBase
from omnibci.core.data.types import SignalChunk

# the data goes into epoch manager with event, and will be detected.
# the data goes into decoder without event channel.
class EpochManager:
    def __init__(self, decoder: DecoderBase, detector: EpochDetectorBase):
        self.decoder = decoder
        self.detector = detector
        self.epoch_data = np.empty((0, 0))
        self.current_chunk_data = None
        self.current_event_id = None

    def _reset(self):
        """Reset internal state."""
        self.epoch_data = np.empty((0, 0))
        self.current_chunk_data = None
        self.current_event_id = None

    def detect_start(self, chunk: SignalChunk) -> Tuple[int, Optional[int]]:
        """
        Detect start events in chunk.

        Returns:
            Tuple of (index, event_id). Index is -1 if no start detected.
        """
        return self.detector.detect_start(chunk)

    def detect_end(self, chunk: SignalChunk) -> Tuple[int, Optional[int]]:
        """
        Detect end events in chunk.

        Returns:
            Tuple of (index, event_id). Index is -1 if no end detected.
        """
        return self.detector.detect_end(chunk)

    def initialize_epoch(self, chunk, event_id: int):
        """Initialize a new epoch with data from the start events onward."""
        start_idx, _ = self.detect_start(chunk)
        if start_idx < 0:
            raise ValueError("Cannot initialize epoch: no start events found")

        self.epoch_data = chunk.data[:, start_idx:]
        self.current_event_id = event_id
        self.current_chunk_data = self.epoch_data
        logging.info(f"Epoch initialized with event_id={event_id}, data_shape={self.epoch_data.shape}")

    def append_chunk(self, chunk: SignalChunk):
        """Append entire chunk to epoch data."""
        self.current_chunk_data = chunk.data
        self.epoch_data = np.append(self.epoch_data, chunk.data, axis=1)

    def append_until_index(self, chunk, index: int):
        """Append chunk data up to specified index."""
        if index < 0:
            raise ValueError(f"Invalid index: {index}")

        data_slice = chunk.data[:, :index]
        self.current_chunk_data = data_slice
        self.epoch_data = np.append(self.epoch_data, data_slice, axis=1)

    def run_pipeline(self, force_recognize: bool = False) -> Tuple[bool, int]:
        """
        Perform recognition on accumulated epoch data.

        Args:
            force: Force recognition even if conditions aren't met

        Returns:
            Tuple of (is_complete, result)
            :param force_recognize:
        """
        # TODO: if force: recognize else: judge recognize frequency.
        if force_recognize:
            return self.decoder.analyse(self.epoch_data, self.current_chunk_data, force_predict=True)
        else:
            # TODO: is pipeline should run ever chuck or longer than some value
            allowed = True
            if allowed:
                return self.decoder.analyse(self.epoch_data, self.current_chunk_data, force_recognize)

        return False, 0


    def clear(self):
        """Clear all epoch data."""
        self._reset()
