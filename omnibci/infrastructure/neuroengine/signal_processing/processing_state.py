"""
SignalProcessor states — refactored to use domain objects.

Changes from original:
  - _emit_result(int) replaced by _complete_trial(Classification).
  - _complete_trial() calls BCISession.record_trial() so the aggregate root
    enforces business rules and accumulates trial history.
  - Domain events are drained from BCISession and published via
    context.event_publisher (DomainEventPublisher), not via a direct import
    of processor.py, removing the circular dependency.

ACL change:
  The end of _complete_trial previously contained a deferred import:
      from omnibci.infrastructure.neuroengine.processor.processor import _publish_domain_event
  This worked around a processor/state circular dependency. Translation now
  lives in DomainEventPublisher, constructed by SignalProcessor and exposed as
  self.event_publisher. States access it through their context without importing
  the processor module.
"""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from omnibci.domain.signal import SignalChunk
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

    def _boundaries(self, chunk: SignalChunk):
        """Read every boundary using the detector's first-marker interface."""
        offset = 0
        samples = chunk.raw_data.shape[1]
        boundaries = []
        while offset < samples:
            remaining = chunk if offset == 0 else replace(chunk, raw_data=chunk.raw_data[:, offset:])
            start_idx, event_id = self.context.detector.detect_start(remaining)
            end_idx, _ = self.context.detector.detect_end(remaining)
            positions = [index for index in (start_idx, end_idx) if index >= 0]
            if not positions:
                break
            index = min(positions)
            if index >= samples - offset:
                raise ValueError("Epoch detector returned a boundary outside the signal chunk")
            boundaries.append((offset + index, index == start_idx, event_id))
            offset += index + 1
        return boundaries

    def _process_chunk(self, chunk: SignalChunk, *, collecting: bool) -> None:
        boundaries = self._boundaries(chunk)
        if not boundaries:
            if collecting:
                complete, classification = self.context.current_epoch.process_chunk(chunk)
                if complete and classification is not None:
                    self._complete_trial(classification)
                    self.context.transition_to_awaiting()
            self.context.pretrigger.push(chunk.data)
            return

        was_collecting = collecting
        opened = False
        for position, (index, starts_epoch, event_id) in enumerate(boundaries):
            if collecting:
                # Epochs opened in this chunk already end at this boundary.
                if not opened:
                    self.context.current_epoch.append_until_index(chunk, index)
                _, classification = self.context.current_epoch.force_decode_at_boundary()
                if classification is not None:
                    self._complete_trial(classification)
                self.context.current_epoch = None
                collecting = False

            if starts_epoch:
                # History holds previous chunks until this whole chunk is done.
                # Every start combines it with its prefix exactly once.
                pre_trigger_data = self.context.pretrigger.take_pre_trigger_data(extra=chunk.data[:, :index])
                # The start sample belongs to this epoch; the next boundary does
                # not. A simultaneous end/start closes the old epoch first.
                end = boundaries[position + 1][0] if position + 1 < len(boundaries) else chunk.raw_data.shape[1]
                segment = chunk if end == chunk.raw_data.shape[1] else replace(chunk, raw_data=chunk.raw_data[:, :end])
                self.context.current_epoch = self.context.new_epoch_processor()
                self.context.current_epoch.initialize_epoch(segment, index, event_id, pre_trigger_data=pre_trigger_data)
                collecting = True
                opened = True

        self.context.pretrigger.push(chunk.data)
        if collecting:
            if not was_collecting:
                self.context.transition_to_assembling()
        elif was_collecting or opened:
            self.context.transition_to_awaiting()

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
            session.record_trial(
                epoch, classification,
                config_revision=getattr(self.context.current_epoch, "config_revision", None),
            )
        except (RuntimeError, ValueError) as exc:
            logger.error("BCISession.record_trial() rejected: %s", exc)
            return

        # Drain domain events and publish via the ACL translator.
        # SignalProcessor creates this publisher; states access it through the
        # context without importing the processor module.
        self.context.event_publisher.publish_all(session.pop_events())


class AwaitingEpochState(ProcessingState):
    """Idle state: drain the data queue and watch for a start trigger."""

    async def run(self) -> None:
        chunk = await self.context.next_chunk()
        if chunk is not None:
            self._process_chunk(chunk, collecting=False)


class AssemblingEpochState(ProcessingState):
    """Active state: accumulate chunks and record completed trials."""

    async def run(self) -> None:
        chunk = await self.context.next_chunk()
        if chunk is not None:
            self._process_chunk(chunk, collecting=True)
