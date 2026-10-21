"""Behavioral tests for the signal-processing state machine."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from tests.processing_helpers import make_processor
from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.shared.events import EventType
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.signal_processing.processing_state import (
    AssemblingEpochState,
    AwaitingEpochState,
    ProcessingState,
)


def _chunk(samples: int = 6, *, start=None, end=None) -> SignalChunk:
    signal = np.arange(2 * samples, dtype=float).reshape(2, samples)
    triggers = np.zeros((1, samples), dtype=float)
    if start is not None:
        triggers[0, start[0]] = start[1]
    if end is not None:
        triggers[0, end] = 255
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
    context.detector.detect_start.side_effect = lambda item: _first_marker(item, starts=True)
    context.detector.detect_end.side_effect = lambda item: _first_marker(item, starts=False)
    context.current_epoch = MagicMock()
    context.new_epoch_processor.return_value = MagicMock()
    return context


def _first_marker(chunk, *, starts):
    selected = (chunk.triggers > 0) & (chunk.triggers < 255) if starts else chunk.triggers == 255
    positions = np.flatnonzero(selected)
    return (int(positions[0]), int(chunk.triggers[positions[0]])) if positions.size else (-1, None)


def _processor():
    processor = make_processor(
        detector=MagicMock(), decoder=MagicMock(), command_queue=MagicMock(),
        data_manager=MagicMock(), event_bus=MagicMock(),
        generation="run", config_revision="processor-revision",
    )
    processor.detector.detect_start.side_effect = lambda item: _first_marker(item, starts=True)
    processor.detector.detect_end.side_effect = lambda item: _first_marker(item, starts=False)
    processor.current_epoch = MagicMock(
        build_epoch=MagicMock(return_value=_epoch()), config_revision="epoch-revision",
    )
    processor.transition_to_assembling()
    return processor


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

        await AwaitingEpochState(context).run()

        pushed = context.pretrigger.push.call_args.args[0]
        np.testing.assert_array_equal(pushed, chunk.data)
        context.new_epoch_processor.assert_not_called()
        context.transition_to_assembling.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_builds_fresh_epoch_with_pretrigger_data(self):
        chunk = _chunk(start=(2, 7))
        pre_trigger_data = np.ones((2, 3))
        epoch_processor = MagicMock()
        context = _context(chunk)
        context.pretrigger.take_pre_trigger_data.return_value = pre_trigger_data
        context.new_epoch_processor.return_value = epoch_processor

        await AwaitingEpochState(context).run()

        extra = context.pretrigger.take_pre_trigger_data.call_args.kwargs["extra"]
        np.testing.assert_array_equal(extra, chunk.data[:, :2])
        np.testing.assert_array_equal(context.pretrigger.push.call_args.args[0], chunk.data)
        context.pretrigger.clear.assert_not_called()
        assert context.current_epoch is epoch_processor
        args = epoch_processor.initialize_epoch.call_args.args
        kwargs = epoch_processor.initialize_epoch.call_args.kwargs
        assert args == (chunk, 2, 7)
        assert kwargs["pre_trigger_data"] is pre_trigger_data
        context.transition_to_assembling.assert_called_once_with()


class TestAssemblingEpochState:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("boundary", [False, True])
    async def test_publication_failure_preserves_active_epoch(self, boundary):
        processor = _processor()
        processor.next_chunk = AsyncMock(return_value=_chunk(end=3) if boundary else _chunk())
        current_epoch = processor.current_epoch
        current_epoch.process_chunk.return_value = (True, _classification())
        current_epoch.force_decode_at_boundary.return_value = (True, _classification())
        processor.event_bus.put_event.side_effect = OSError("publisher failed")

        with pytest.raises(OSError, match="publisher failed"):
            await processor._current_state.run()

        assert processor.current_epoch is current_epoch
        assert processor._current_state is processor._assembling_state
        assert processor.completed_trials == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("boundary", [False, True])
    async def test_completion_publishes_without_any_lifecycle_commands(self, boundary):
        processor = _processor()
        processor.next_chunk = AsyncMock(return_value=_chunk(end=3) if boundary else _chunk())
        classification = _classification()
        processor.current_epoch.process_chunk.return_value = (True, classification)
        processor.current_epoch.force_decode_at_boundary.return_value = (True, classification)

        await processor._current_state.run()

        assert processor.current_epoch is None
        assert processor._current_state is processor._awaiting_state
        assert processor.completed_trials == 1
        event = processor.event_bus.put_event.call_args.args[0]
        assert event.type is EventType.TrialCompleted
        assert event.data.classification is classification

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

        await state.run()

        assert context.current_epoch.process_chunk.call_args.args == (chunk,)
        context.complete_trial.assert_not_called()
        context.transition_to_awaiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_normal_chunk_completes_trial_and_returns_to_awaiting(self):
        chunk = _chunk()
        classification = _classification()
        context = _context(chunk)
        context.current_epoch.process_chunk.return_value = (True, classification)
        state = AssemblingEpochState(context)

        await state.run()

        context.complete_trial.assert_called_once_with(classification)
        context.transition_to_awaiting.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_end_without_result_returns_to_awaiting(self):
        chunk = _chunk(end=3)
        context = _context(chunk)
        previous_epoch = context.current_epoch
        context.current_epoch.force_decode_at_boundary.return_value = (False, None)
        state = AssemblingEpochState(context)

        await state.run()

        assert previous_epoch.append_until_index.call_args.args == (chunk, 3)
        previous_epoch.force_decode_at_boundary.assert_called_once_with()
        context.complete_trial.assert_not_called()
        context.transition_to_awaiting.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_end_and_start_in_same_chunk_complete_and_reopen_epoch(self):
        chunk = _chunk(start=(4, 9), end=2)
        classification = _classification()
        pre_trigger_data = np.ones((2, 2))
        next_epoch = MagicMock()
        context = _context(chunk)
        context.current_epoch.force_decode_at_boundary.return_value = (True, classification)
        context.pretrigger.take_pre_trigger_data.return_value = pre_trigger_data
        context.new_epoch_processor.return_value = next_epoch
        state = AssemblingEpochState(context)

        await state.run()

        context.complete_trial.assert_called_once_with(classification)
        context.pretrigger.take_pre_trigger_data.assert_called_once()
        assert context.current_epoch is next_epoch
        args = next_epoch.initialize_epoch.call_args.args
        kwargs = next_epoch.initialize_epoch.call_args.kwargs
        assert args == (chunk, 4, 9)
        assert kwargs["pre_trigger_data"] is pre_trigger_data
        context.transition_to_awaiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_start_without_result_still_reopens_epoch(self):
        chunk = _chunk(start=(3, 8))
        next_epoch = MagicMock()
        context = _context(chunk)
        previous_epoch = context.current_epoch
        previous_epoch.force_decode_at_boundary.return_value = (False, None)
        context.new_epoch_processor.return_value = next_epoch
        state = AssemblingEpochState(context)

        await state.run()

        assert previous_epoch.append_until_index.call_args.args == (chunk, 3)
        context.complete_trial.assert_not_called()
        assert context.current_epoch is next_epoch
        context.transition_to_awaiting.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_start_completes_previous_trial_before_reopening(self):
        chunk = _chunk(start=(3, 8))
        classification = _classification()
        next_epoch = MagicMock()
        context = _context(chunk)
        previous_epoch = context.current_epoch
        previous_epoch.force_decode_at_boundary.return_value = (True, classification)
        context.new_epoch_processor.return_value = next_epoch
        state = AssemblingEpochState(context)

        await state.run()

        context.complete_trial.assert_called_once_with(classification)
        assert context.current_epoch is next_epoch


class TestCompleteTrial:
    def test_returns_published_completion_with_epoch_revision(self):
        processor = _processor()

        completion = processor.complete_trial(_classification())

        assert processor.completed_trials == 1
        event = processor.event_bus.put_event.call_args.args[0]
        assert event.type is EventType.TrialCompleted
        assert completion is event.data
        assert completion.generation == "run"
        assert completion.trial_number == 1
        assert completion.config_revision == "epoch-revision"
        processor.current_epoch.build_epoch.assert_not_called()

    def test_trial_numbers_increase_without_retaining_trial_history(self):
        processor = _processor()
        first = processor.complete_trial(_classification())
        second = processor.complete_trial(_classification(event_id=9))

        assert (first.trial_number, second.trial_number) == (1, 2)
        assert first.generation == second.generation == processor.generation
        assert processor.completed_trials == 2
        assert not hasattr(processor, "session_runtime")

    def test_failed_publication_does_not_advance_trial_count(self):
        processor = _processor()
        processor.event_bus.put_event.side_effect = OSError("publisher failed")

        with pytest.raises(OSError, match="publisher failed"):
            processor.complete_trial(_classification())

        assert processor.completed_trials == 0


def test_processor_uses_snapshot_generation_or_creates_one_stable_fallback():
    def build(snapshot=None):
        return make_processor(
            detector=MagicMock(), decoder=MagicMock(), command_queue=MagicMock(),
            data_manager=MagicMock(), event_bus=MagicMock(), settings_snapshot=snapshot,
        )

    configured = build({"CONFIG.generation": "configured-generation"})
    assert configured.generation == "configured-generation"
    first, second = build(), build()
    assert isinstance(first.generation, str) and first.generation
    assert first.generation != second.generation
