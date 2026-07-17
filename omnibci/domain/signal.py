#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-18 10:41
"""
SignalChunk — domain value object.

A raw slice of acquired signal — the upstream stage of the
SignalChunk -> Epoch processing chain (Epoch is downstream). It carries the
acquisition-domain convention that the last channel is the trigger channel:
``data`` is every row but the last and ``triggers`` is the last row.

Unlike Epoch, the payload array is NOT copied or made read-only: chunks flow
on the acquisition hot path (one per read), so a per-chunk copy would tax
throughput for no benefit. ``frozen=True`` protects the field bindings only;
the producer hands over ownership of ``raw_data`` and must not mutate it
after construction.
"""
from dataclasses import dataclass

from numpy import typing as npt


@dataclass(frozen=True)
class SignalChunk:
    """One acquired chunk: shape (channels + 1, samples), last row = triggers.

    Attributes:
        timestamp:   Producer-side acquisition timestamp (the built-in
                     acquisition worker stamps ``time.time_ns()``).
        sequence_id: Monotonic chunk counter within one acquisition run.
        raw_data:    Signal matrix including the trailing trigger row.
    """
    timestamp: float
    sequence_id: int
    raw_data: npt.NDArray

    def __post_init__(self) -> None:
        if self.raw_data.ndim != 2:
            raise ValueError(
                f"SignalChunk.raw_data must be 2-D (channels+trigger x samples), "
                f"got shape {self.raw_data.shape}"
            )
        if self.raw_data.shape[0] < 2:
            raise ValueError(
                f"SignalChunk.raw_data needs at least 2 rows "
                f"(>= 1 signal channel + 1 trigger row), got {self.raw_data.shape[0]}"
            )

    @property
    def data(self) -> npt.NDArray:
        """Signal rows only — everything except the trailing trigger row."""
        return self.raw_data[:-1, :]

    @property
    def triggers(self) -> npt.NDArray:
        """The trigger channel — by convention the last row."""
        return self.raw_data[-1, :]
