#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 09:45

import logging
import numpy as np
from typing import Optional, Tuple

from interfaces.algorithm import DecoderBase, SegmenterBase
from core.data.types import SignalChunk


class EpochManager:
    def __init__(self, decoder: DecoderBase, strategy: SegmenterBase):
        self.decoder = decoder
        self.strategy = strategy
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
        return self.strategy.detect_start(chunk)

    def detect_end(self, chunk: SignalChunk) -> Tuple[int, Optional[int]]:
        """
        Detect end events in chunk.

        Returns:
            Tuple of (index, event_id). Index is -1 if no end detected.
        """
        return self.strategy.detect_end(chunk)

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

    def analyse(self, force_recognize: bool = False) -> Tuple[bool, int]:
        """
        Perform recognition on accumulated epoch data.

        Args:
            force: Force recognition even if conditions aren't met

        Returns:
            Tuple of (is_complete, result)
            :param force_recognize:
        """
        # TODO: if force: recognize else: judge recognize frequency segmenter.
        if force_recognize:
            return self.decoder.analyse(self.epoch_data, self.current_chunk_data, force_predict=True)
        else:
            allowed = self.strategy.allowed_decoder_analyse()
            if allowed:
                return self.decoder.analyse(self.epoch_data, self.current_chunk_data, force_recognize)

        return False, 0


    def clear(self):
        """Clear all epoch data."""
        self._reset()

#     def start_new_epoch(self, chunk:SignalChunk):
#         start_idx, event_id = self.segmenter.detect_start(chunk)
#         if start_idx >= 0:
#             valid_data = chunk.data[:, start_idx:]
#             self.epoch_data = valid_data
#             self.current_event_id = event_id
#             self.current_chunk_data = valid_data
#
#     def begin_recognition(self, chunk:SignalChunk) -> bool:
#         index, event_id = self.segmenter.detect_start(chunk)
#         if index >= 0:
#             print(f"EpochManager: begin_recognition, index={index}, events={event_id}")
#             self.current_event_id = event_id
#             return True
#         else:
#             return False
#
#     def end_recognition(self, chunk:SignalChunk) -> bool:
#         index, events = self.segmenter.detect_end(chunk)
#         if index >= 0:
#             print(f"EpochManager: end_recognition, index={index}, events={events}")
#             return True
#         else:
#             return False
#
#
#     # def set_epoch_head_data(self, data_packet):
#     #     index, events = self.decoder.trial_start_position(data_packet)
#     #     if index >= 0:
#     #         new_data = data_packet.data[0:-1, index:]  # Slice rows
#     #         self.epoch_data = new_data
#     #     else:
#     #         raise ValueError("Epoch head index is not set.")
#
#     def append_tail_data(self, chunk: SignalChunk):
#         index, events = self.segmenter.detect_end(chunk)
#         if index >= 0:
#             new_data = chunk.data[:, 0:index]  # Slice rows
#             self.epoch_data = np.append(self.epoch_data, new_data, axis=1)
#         else:
#             raise ValueError("Epoch tail index is not set.")
#
# #   append events pre data
#     def append_data_before_begin_event(self, chunk: SignalChunk):
#         index, events = self.segmenter.detect_start(chunk)
#         if index >= 0:
#             new_data = chunk.data[:, 0:index]
#             self.epoch_data = np.append(self.epoch_data, new_data, axis=1)
#         else:
#             raise ValueError("Epoch head index is not set.")
#
#     def append_data(self, chunk: SignalChunk):
#         self.current_chunk_data = chunk.data # Slice rows
#         self.epoch_data = np.append(self.epoch_data, self.current_chunk_data, axis=1)
#
#
#     def analysis(self, force_predict=False) -> (bool, npt.NDArray):
#         return self.decoder.analyse(self.epoch_data, self.current_chunk_data, force_predict)
#
#     def clear(self):
#         self.epoch_data = np.empty((0, 0))
#         self.current_data_packet = None
#         self.current_event_id = None
