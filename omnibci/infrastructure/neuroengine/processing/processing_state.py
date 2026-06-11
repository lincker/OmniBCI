"""
SignalProcessor states — refactored to use domain objects.

Changes from original:
  - _emit_result(int) replaced by _complete_trial(Classification).
  - _complete_trial() calls BCISession.record_trial() so the aggregate root
    enforces business rules and accumulates trial history.
  - Domain events are drained from BCISession and published via
    context.event_publisher (DomainEventPublisher), not via a direct import
    of processor.py — 循环依赖因此消除。

ACL change:
  原来在 _complete_trial 末尾有一个延迟 import:
      from omnibci.infrastructure.neuroengine.processor.processor import _publish_domain_event
  这是为了绕过 processor ↔ state 循环依赖而打的补丁。现在翻译逻辑已移入
  DomainEventPublisher，SignalProcessor 在 __init__ 中构造并赋值给
  self.event_publisher，states 通过 self.context.event_publisher 访问，
  无需再 import processor 模块。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, TYPE_CHECKING

from omnibci.shared.signal import SignalChunk
from omnibci.domain.classification import Classification

if TYPE_CHECKING:
    from omnibci.domain.session import BCISession

logger = logging.getLogger(__name__)


class ProcessingState:
    """Base class for signal-processing states."""

    def __init__(self, context) -> None:
        self.context = context

    async def run(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# AwaitingEpochState
# ---------------------------------------------------------------------------

class AwaitingEpochState(ProcessingState):
    """Idle state: drain the data queue and watch for a start trigger."""

    async def run(self) -> None:
        if self.context.data_manager.empty():
            await asyncio.sleep(0.01)
            return

        chunk: SignalChunk = self.context.data_manager.get_data()
        start_idx, event_id = self.context.detector.detect_start(chunk)

        if start_idx < 0:
            # No trigger yet: this whole chunk is pre-trigger history. Keep the
            # trailing baseline_samples so a future start can claim its baseline.
            self.context.pretrigger.push(chunk.data)
            return

        logger.info("Start event detected: index=%d, event_id=%d", start_idx, event_id)
        # Baseline = trailing samples ending exactly at the trigger: whatever the
        # ring buffer accumulated while idle, plus this chunk's pre-trigger slice.
        baseline = self.context.pretrigger.take_baseline(extra=chunk.data[:, :start_idx])
        self.context.pretrigger.clear()
        # A fresh EpochProcessor per epoch — no reused/cleared buffer.
        self.context.current_epoch = self.context.new_epoch_processor()
        self.context.current_epoch.initialize_epoch(chunk, start_idx, event_id, baseline=baseline)
        self.context.transition_to_assembling()


# ---------------------------------------------------------------------------
# AssemblingEpochState
# ---------------------------------------------------------------------------

class AssemblingEpochState(ProcessingState):
    """
    Active state: accumulate chunks and run the decoder.

    The key behavioural change: instead of emitting a bare int to the EventBus,
    each completed trial is recorded on the BCISession aggregate root, which
    validates the result, appends it to session history, and raises a
    TrialCompleted domain event.  This state then delegates translation of that
    domain event to context.event_publisher (DomainEventPublisher / ACL).
    """

    async def run(self) -> None:
        if self.context.data_manager.empty():
            await asyncio.sleep(0.01)
            return

        chunk: SignalChunk = self.context.data_manager.get_data()

        end_idx, _ = self.context.detector.detect_end(chunk)
        start_idx, new_event_id = self.context.detector.detect_start(chunk)

        has_end_event = end_idx >= 0
        has_start_event = start_idx >= 0

        if not has_end_event and not has_start_event:
            self._process_normal_data(chunk)
            return

        if has_end_event:
            self._process_end_event(chunk, end_idx, has_start_event, start_idx, new_event_id)
            return

        if has_start_event:
            self._process_unexpected_start(chunk, start_idx, new_event_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process_normal_data(self, chunk: SignalChunk) -> None:
        # EpochProcessor ingests the chunk and decides (via should_decode) whether
        # to run the pipeline this tick.
        is_complete, classification = self.context.current_epoch.process_chunk(chunk)
        if is_complete and classification is not None:
            self._complete_trial(classification)
            self.context.transition_to_awaiting()

    def _process_end_event(
        self,
        chunk: SignalChunk,
        end_idx: int,
        has_start_event: bool,
        start_idx: int,
        new_event_id: Optional[int],
    ) -> None:
        self.context.current_epoch.append_until_index(chunk, end_idx)
        _, classification = self.context.current_epoch.run_pipeline(force_recognize=True)
        logger.info("End event forced recognition: %s", classification)

        if classification is not None:
            self._complete_trial(classification)

        if has_start_event:
            logger.info("New epoch starts in same chunk: index=%d", start_idx)
            # Reopen = a new epoch → fresh EpochProcessor (fresh decoder state).
            baseline = self.context.baseline_from_chunk(chunk, start_idx)
            self.context.current_epoch = self.context.new_epoch_processor()
            self.context.current_epoch.initialize_epoch(
                chunk, start_idx, new_event_id, baseline=baseline
            )
            # Stay in AssemblingEpochState
        else:
            self.context.transition_to_awaiting()

    def _process_unexpected_start(
        self,
        chunk: SignalChunk,
        start_idx: int,
        event_id: int,
    ) -> None:
        logger.warning("Unexpected start event during collection — forcing recognition")
        self.context.current_epoch.append_until_index(chunk, start_idx)
        _, classification = self.context.current_epoch.run_pipeline(force_recognize=True)
        logger.info("Forced recognition (unexpected start): %s", classification)

        if classification is not None:
            self._complete_trial(classification)

        # Reopen = a new epoch → fresh EpochProcessor (fresh decoder state).
        baseline = self.context.baseline_from_chunk(chunk, start_idx)
        self.context.current_epoch = self.context.new_epoch_processor()
        self.context.current_epoch.initialize_epoch(
            chunk, start_idx, event_id, baseline=baseline
        )

    def _complete_trial(self, classification: Classification) -> None:
        """
        Hand the Classification to the BCISession aggregate root.

        BCISession enforces invariants, records the Trial, and raises a
        TrialCompleted domain event.  We then hand all pending domain events
        to context.event_publisher which translates and delivers them to the
        EventBus (API WebSocket, stimulation feedback, etc.).
        """
        session: BCISession = self.context.session

        # Build Epoch VO from the current epoch's buffer before it is dropped
        try:
            epoch = self.context.current_epoch.build_epoch()
        except RuntimeError as exc:
            logger.error("Could not build Epoch for trial: %s", exc)
            return

        # Delegate to aggregate root — enforces business rules
        try:
            trial_completed = session.record_trial(epoch, classification)
        except (RuntimeError, ValueError) as exc:
            logger.error("BCISession.record_trial() rejected: %s", exc)
            return

        # Drain domain events and publish via the ACL translator.
        # DomainEventPublisher 由 SignalProcessor 在 __init__ 中创建并赋值给
        # self.event_publisher，states 通过 context 访问，无需 import processor。
        self.context.event_publisher.publish_all(session.pop_events())
