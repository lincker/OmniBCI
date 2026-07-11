#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 10:53
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Tuple

from numpy import typing as npt

from omnibci.shared.signal import SignalChunk


@dataclass(frozen=True)
class FilterContext:
    """Per-run metadata handed to each Filter (immutable)."""
    sample_rate: int
    forced: bool = False
    event_id: Optional[int] = None
    # Pre-trigger segment (channels × n) captured for this epoch, or None when no
    # baseline was configured. A baseline-correction Filter reads it from here so
    # the main data stream stays the analysis window only.
    baseline: Optional[npt.NDArray] = None


class Filter(ABC):
    """
    A single ordered preprocessing stage in a ProcessingPipeline.

    Takes the current epoch window, transforms it (band-pass, baseline,
    downsample, channel selection, …) and returns it for the next stage.
    Pure transform: no termination decision.
    """

    @abstractmethod
    def apply(self, data: npt.NDArray, ctx: FilterContext) -> npt.NDArray:
        """Transform *data* (channels × samples) and return the result."""


@dataclass(frozen=True)
class Prediction:
    """A decoder's output for one epoch window (immutable)."""
    target_id: int
    confidence: Optional[float] = None


class Decoder(ABC):
    """
    Turns a (filtered) epoch window into a recognized target.

    Contract (general — fits discrete SSVEP/P300/MI and continuous decoders):
      - should_decode(data) — cheap gate: are the conditions ready
        for *data* yet? The engine calls this each chunk and only runs the
        pipeline when it returns True.
      - decode(data) — produce a Prediction, or None if not ready / not
        confident. Pure function of *data*.
    """


    def __init__(self):
        super().__init__()

    @abstractmethod
    def should_decode(self, window: npt.NDArray) -> bool:
        """
        Cheap precondition gate — is it worth attempting a decode on *window* now?
        Looks only at the accumulated epoch window. Override with a *cheap* check
        (e.g. enough samples / repetitions) so the engine skips the pipeline until
        the epoch is ready.
        """

    @abstractmethod
    def decode(self, window: npt.NDArray, latest: npt.NDArray) -> Optional[Prediction]:
        """
        Decode → Prediction, or None if the decoder declines (not ready / not
        confident).

        Args:
            window: the whole accumulated epoch buffer (channels × samples).
                    Whole-window decoders (SSVEP CCA, ERP averaging) use this.
            latest: the most-recently appended chunk (the delta). Incremental /
                    online decoders use this (+ their per-epoch state from
                    ``for_new_epoch``) to avoid reprocessing the whole buffer.

        Either input may be ignored depending on the decoder's style.
        """

    def for_new_epoch(self) -> "Decoder":
        """
        Return the decoder instance to use for a freshly opened epoch.

        The engine creates one EpochProcessor per epoch and calls this to obtain
        that epoch's decoder. The default returns ``self`` — correct for stateless
        decoders (which recompute from the full epoch buffer each call), so one
        shared instance is reused with zero cost and full concurrency safety.

        A decoder that keeps mutable per-epoch state across ``should_decode`` calls
        must override this to return a **fresh** instance that shares the heavy,
        immutable model (templates/weights) but has its own working state. This
        keeps per-epoch state isolated (no leakage between trials, safe for
        concurrent/overlapping epochs) without re-paying model construction.
        """
        return self


class EpochDetector(ABC):
    """detect epoch start and end."""

    """return（index, event_id). If not found, return（-1，0）"""
    @abstractmethod
    def detect_start(self, chunk: SignalChunk, start_event_set=None) -> Tuple[int, int]:
        pass

    """return（index, event_id). If not found, return（-1，0）"""
    @abstractmethod
    def detect_end(self, chunk: SignalChunk, end_event_set=None) -> Tuple[int, int]:
        pass
