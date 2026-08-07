"""Behavioral tests for the signal-processing state machine."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.domain.events import TrialCompleted
from omnibci.domain.session import BCISession
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.neuroengine.processing.processing_state import (
    AssemblingEpochState,
    AwaitingEpochState,
    ProcessingState,
)


def _chunk(samples: int = 6) -> SignalChunk:
    signal = np.arange(2 * samples, dtype=float).reshape(2, samples)
    triggers = np.zeros((1, samples), dtype=float)
    return SignalChunk(
        timestamp=1.0,
        sequence_id=1,
        raw_data=np.vstack((signal, triggers)),
    )


def _epoch(event_id: int = 7) -> Epoch:
    return Epoch(
        data=np.ones((2, 5)),
        event_id=event_id,
        sample_rate=1000,
        channel_count=2,
    )


def _classification(event_id: int = 7) -> Classification:
    return Classification(target_id=3, event_id=event_id, confidence=0.9)


def _context(chunk: SignalChunk | None):
    context = MagicMock()
    context.next_chunk = AsyncMock(return_value=chunk)
    context.detector.detect_start.return_value = (-1, None)
    context.detector.detect_end.return_value = (-1, None)
    context.current_epoch = MagicMock()
    context.new_epoch_processor.return_value = MagicMock()
    return context


class TestProcessingStateContract:
    @pytest.mark.asyncio
    async def test_base_state_requires_implementation(self):
        with pytest.raises(NotImplementedError):
            await ProcessingState(MagicMock()).run()


class TestAwaitingEpochState:
    @pytest.mark.asyncio
    async def test_timeout_leaves_state_unchanged(self):
        context = _context(None)

        await AwaitingEpochState(context).run()

        context.detector.detect_start.assert_not_called()
        context.pretrigger.push.assert_not_called()
        context.transition_to_assembling.assert_not_called()

    @pytest.mark.asyncio
    async def test_chunk_without_start_is_saved_as_pretrigger_history(self):
        chunk = _chunk()
        context = _context(chunk)
        context.detector.detect_start.return_value = (-1, None)

        await AwaitingEpochState(context).run()

        pushed = context.pretrigger.push.call_args.args[0]
        np.testing.assert_array_equal(pushed, chunk.data)
        context.new_epoch_processor.assert_not_called()
        context.transition_to_assembling.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_builds_fresh_epoch_with_pretrigger_baseline(self):
        chunk = _chunk()
        baseline = np.ones((2, 3))
        epoch_processor = MagicMock()
        context = _context(chunk)
        context.detector.detect_start.return_value = (2, 7)
        context.pretrigger.take_baseline.return_value = baseline
        context.new_epoch_processor.return_value = epoch_processor

        await AwaitingEpochState(context).run()

        extra = context.pretrigger.take_baseline.call_args.kwargs["extra"]
        np.testing.assert_array_equal(extra, chunk.data[:, :2])
        context.pretrigger.clear.assert_called_once_with()
        assert context.current_epoch is epoch_processor
        args = epoch_processor.initialize_epoch.call_args.args
        kwargs = epoch_processor.initialize_epoch.call_args.kwargs
        assert args == (chunk, 2, 7)
        assert kwargs["baseline"] is baseline
        context.transition_to_assembling.assert_called_once_with()


class TestAssemblingEpochState:
    @pytest.mark.asyncio
    async def test_timeout_leaves_active_epoch_unchanged(self):
        context = _context(None)

        await AssemblingEpochState(context).run()

        context.detector.detect_end.assert_not_called()
        context.detector.detect_start.assert_not_called()
        context.current_epoch.process_chunk.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_chunk_continues_when_decode_is_incomplete(self):
        chunk = _chunk()
        context = _context(chunk)
        context.current_epoch.process_chunk.return_value = (False, None)
        state = AssemblingEpochState(context)
        state._complete_trial = MagicMock()

        await state.run()

        assert context.current_epoch.process_chunk.call_args.args == (chunk,)
        state._complete_trial.assert_not_called()
        context.transition_to_awaiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_chunk_completes_trial_and_returns_to_awaiting(self):
        chunk = _chunk()
        classification = _classification()
        context = _context(chunk)
        context.current_epoch.process_chunk.return_value = (True, classification)
        state = AssemblingEpochState(context)
        state._complete_trial = MagicMock()

        await state.run()

        state._complete_trial.assert_called_once_with(classification)
        context.transition_to_awaiting.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_end_without_result_returns_to_awaiting(self):
        chunk = _chunk()
        context = _context(chunk)
        context.detector.detect_end.return_value = (3, None)
        context.current_epoch.force_recognize.return_value = (False, None)
        state = AssemblingEpochState(context)
        state._complete_trial = MagicMock()

        await state.run()

        assert context.current_epoch.append_until_index.call_args.args == (chunk, 3)
        context.current_epoch.force_recognize.assert_called_once_with()
        state._complete_trial.assert_not_called()
        context.transition_to_awaiting.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_end_and_start_in_same_chunk_complete_and_reopen_epoch(self):
        chunk = _chunk()
        classification = _classification()
        baseline = np.ones((2, 2))
        next_epoch = MagicMock()
        context = _context(chunk)
        context.detector.detect_end.return_value = (2, None)
        context.detector.detect_start.return_value = (4, 9)
        context.current_epoch.force_recognize.return_value = (True, classification)
        context.baseline_from_chunk.return_value = baseline
        context.new_epoch_processor.return_value = next_epoch
        state = AssemblingEpochState(context)
        state._complete_trial = MagicMock()

        await state.run()

        state._complete_trial.assert_called_once_with(classification)
        context.baseline_from_chunk.assert_called_once_with(chunk, 4)
        assert context.current_epoch is next_epoch
        args = next_epoch.initialize_epoch.call_args.args
        kwargs = next_epoch.initialize_epoch.call_args.kwargs
        assert args == (chunk, 4, 9)
        assert kwargs["baseline"] is baseline
        context.transition_to_awaiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_start_without_result_still_reopens_epoch(self):
        chunk = _chunk()
        next_epoch = MagicMock()
        context = _context(chunk)
        previous_epoch = context.current_epoch
        context.detector.detect_start.return_value = (3, 8)
        previous_epoch.force_recognize.return_value = (False, None)
        context.new_epoch_processor.return_value = next_epoch
        state = AssemblingEpochState(context)
        state._complete_trial = MagicMock()

        await state.run()

        assert previous_epoch.append_until_index.call_args.args == (chunk, 3)
        state._complete_trial.assert_not_called()
        assert context.current_epoch is next_epoch
        context.transition_to_awaiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_start_completes_previous_trial_before_reopening(self):
        chunk = _chunk()
        classification = _classification()
        next_epoch = MagicMock()
        context = _context(chunk)
        previous_epoch = context.current_epoch
        context.detector.detect_start.return_value = (3, 8)
        previous_epoch.force_recognize.return_value = (True, classification)
        context.new_epoch_processor.return_value = next_epoch
        state = AssemblingEpochState(context)
        state._complete_trial = MagicMock()

        await state.run()

        state._complete_trial.assert_called_once_with(classification)
        assert context.current_epoch is next_epoch


class TestCompleteTrial:
    def test_records_trial_and_publishes_domain_event(self):
        session = BCISession(session_id="session-1")
        session.start()
        session.pop_events()
        publisher = MagicMock()
        context = SimpleNamespace(
            current_epoch=MagicMock(build_epoch=MagicMock(return_value=_epoch())),
            session=session,
            event_publisher=publisher,
        )

        AssemblingEpochState(context)._complete_trial(_classification())

        assert session.trial_count == 1
        events = publisher.publish_all.call_args.args[0]
        assert len(events) == 1
        assert isinstance(events[0], TrialCompleted)
        assert events[0].session_id == "session-1"

    def test_build_failure_does_not_record_or_publish(self):
        current_epoch = MagicMock()
        current_epoch.build_epoch.side_effect = RuntimeError("no active epoch")
        session = MagicMock()
        publisher = MagicMock()
        context = SimpleNamespace(
            current_epoch=current_epoch,
            session=session,
            event_publisher=publisher,
        )

        AssemblingEpochState(context)._complete_trial(_classification())

        session.record_trial.assert_not_called()
        publisher.publish_all.assert_not_called()

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("session is idle"), ValueError("event id mismatch")],
    )
    def test_session_rejection_does_not_publish(self, error):
        session = MagicMock()
        session.record_trial.side_effect = error
        publisher = MagicMock()
        context = SimpleNamespace(
            current_epoch=MagicMock(build_epoch=MagicMock(return_value=_epoch())),
            session=session,
            event_publisher=publisher,
        )

        AssemblingEpochState(context)._complete_trial(_classification())

        session.pop_events.assert_not_called()
        publisher.publish_all.assert_not_called()
