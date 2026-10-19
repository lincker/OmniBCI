"""A staged pipeline changes only between epochs, with truthful receipts."""

from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.processing_helpers import make_processor
from omnibci.application.events import CommandFailureKind, CommandRequest
from omnibci.domain.ports.algorithm import Decoder, EpochDetector, Filter, Prediction
from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.signal_processing import configuration_transactions as transactions_module
from omnibci.infrastructure.signal_processing import signal_processor as module
from omnibci.infrastructure.signal_processing.pipeline_configuration import ConfiguredPipeline
from omnibci.shared.events import EventType


class ValueDecoder(Decoder):
    def should_decode(self, window):
        return True

    def decode(self, window, latest_samples):
        return Prediction(int(window[0, 0]))


class Add(Filter):
    def __init__(self, value):
        self.value = value

    def apply(self, data, ctx):
        return data + self.value


class Multiply(Filter):
    def __init__(self, value):
        self.value = value

    def apply(self, data, ctx):
        return data * self.value


class Markers(EpochDetector):
    def detect_start(self, chunk):
        marks = chunk.raw_data[-1]
        positions = np.flatnonzero((marks > 0) & (marks < 255))
        return (int(positions[0]), int(marks[positions[0]])) if positions.size else (-1, 0)

    def detect_end(self, chunk):
        positions = np.flatnonzero(chunk.raw_data[-1] == 255)
        return (int(positions[0]), 255) if positions.size else (-1, 0)


def _chunk(markers, sequence=1):
    return SignalChunk(1.0, sequence, np.vstack((np.ones(len(markers)), markers)))


@pytest.fixture
def configured(monkeypatch):
    events = []
    current = {"CONFIG.revision": "old", "CONFIG.generation": "run"}
    processor = make_processor(
        detector=Markers(), decoder=ValueDecoder(), filters=[Add(1)],
        command_queue=Queue(), data_manager=DataManager(buffer=Queue()),
        event_bus=SimpleNamespace(put_event=events.append),
        sample_rate=250, generation="run", config_revision="old", settings_snapshot=current,
    )
    replacement = ConfiguredPipeline(ValueDecoder(), (Add(2), Multiply(3)),
                                      {"CONFIG.revision": "new", "CONFIG.generation": "run"})
    builder = MagicMock(return_value=replacement)
    monkeypatch.setattr(transactions_module, "build_pipeline_update", builder)
    return processor, events, replacement, builder


def _stage(processor, command_id="update", **overrides):
    payload = {"settings_snapshot": {"CONFIG.revision": "new", "CONFIG.generation": "run"},
               "expected_revision": "old", "generation": "run"}
    payload.update(overrides)
    processor._execute((module.SignalProcessorCommand.stage_configuration,
                        CommandRequest(command_id, payload)))


def _cancel(processor, target="update", generation="run"):
    processor._execute((module.SignalProcessorCommand.cancel_configuration,
                        CommandRequest("cancel", {"command_id": target, "generation": generation})))


def _results(events, command=None):
    return [event.data for event in events if event.type is EventType.CommandResult
            and (command is None or event.data.command == command)]


async def _feed(processor, markers, sequence=1):
    processor.data_manager.put_data(_chunk(markers, sequence))
    await processor._current_state.run()


def test_idle_update_acknowledges_only_after_objects_and_revision_are_installed(configured):
    processor, events, replacement, builder = configured
    _stage(processor)
    assert processor._decoder is replacement.decoder
    assert processor._filters == list(replacement.filters)
    assert processor.config_revision == "new"
    assert processor.new_epoch_processor().config_revision == "new"
    result, = _results(events)
    assert result.ok
    assert result.data == {"status": "applied", "command_id": "update",
                           "applied_revision": "new", "generation": "run"}
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["same_chunk", "unexpected_start", "normal_completion"])
async def test_active_epoch_keeps_objects_and_revision_until_natural_boundary(configured, boundary):
    processor, events, replacement, _ = configured
    await _feed(processor, [7, 0, 0])
    old_epoch = processor.current_epoch
    old_decoder = processor._decoder
    _stage(processor)
    assert processor.config_revision == "old"
    assert processor._decoder is old_decoder
    assert old_epoch.pipeline.filters[0].value == 1
    assert not _results(events)
    if boundary == "same_chunk":
        await _feed(processor, [0, 255, 0, 9, 0], 2)
    elif boundary == "unexpected_start":
        await _feed(processor, [0, 0, 9, 0], 2)
    else:
        await _feed(processor, [0, 0, 0], 2)
        assert processor.current_epoch is None
        await _feed(processor, [9, 0, 0], 3)
    assert processor.current_epoch is not old_epoch
    assert processor.current_epoch.decoder is replacement.decoder
    assert processor.current_epoch.config_revision == "new"
    assert old_epoch.config_revision == "old"
    assert processor.config_revision == "new"
    trial_events = [event for event in events if event.type is EventType.TrialCompleted]
    assert trial_events[0].data.config_revision == "old"
    assert trial_events[0].data.classification.target_id == 2
    receipt_event = next(event for event in events if event.type is EventType.CommandResult)
    assert events.index(trial_events[0]) < events.index(receipt_event)
    await _feed(processor, [0, 255, 0], 4)
    trials = [event.data for event in events if event.type is EventType.TrialCompleted]
    assert [(trial.config_revision, trial.classification.target_id) for trial in trials] == [("old", 2), ("new", 9)]


def test_preparation_failure_retains_previous_objects_and_revision(configured):
    processor, events, _, builder = configured
    old_decoder = processor._decoder
    builder.side_effect = ValueError("invalid replacement")
    _stage(processor)
    assert processor._decoder is old_decoder
    assert processor.config_revision == "old"
    assert processor._configuration_transactions.pending is None
    assert not _results(events)[0].ok


@pytest.mark.parametrize("overrides", [{"generation": "stale"}, {"expected_revision": "stale"}])
def test_stale_command_is_rejected_before_plugin_construction(configured, overrides):
    processor, events, _, builder = configured
    _stage(processor, **overrides)
    builder.assert_not_called()
    assert not _results(events)[0].ok
    assert processor.config_revision == "old"


@pytest.mark.asyncio
async def test_one_pending_update_and_duplicate_id_never_construct_twice(configured):
    processor, events, _, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _stage(processor)
    assert not _results(events)
    _stage(processor, "other")
    assert not _results(events)[0].ok
    builder.assert_called_once()
    await _feed(processor, [0, 255, 0], 2)
    _stage(processor)
    replies = [result for result in _results(events) if result.command_id == "update"]
    assert len(replies) == 2 and replies[0] == replies[1]
    builder.assert_called_once()


@pytest.mark.asyncio
async def test_conflicting_duplicate_preserves_pending_update_until_boundary(configured):
    processor, events, replacement, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _stage(processor, settings_snapshot={"CONFIG.revision": "different"})
    conflict, = _results(events)
    assert not conflict.ok
    assert conflict.error_code == "PROCESSING_CONFIGURATION_CONFLICT"
    assert conflict.failure_kind is CommandFailureKind.REJECTED
    assert conflict.data is None
    _stage(processor)
    assert _results(events) == [conflict]
    await _feed(processor, [0, 255, 9, 0], 2)
    assert processor.config_revision == "new"
    assert processor.current_epoch.config_revision == "new"
    assert processor.current_epoch.decoder is replacement.decoder
    assert _results(events)[-1].data["status"] == "applied"
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["applied", "rejected", "cancelled", "busy"])
async def test_conflicting_duplicate_preserves_terminal_receipt_and_original_retry(configured, outcome):
    processor, events, _, builder = configured
    if outcome in {"cancelled", "busy"}:
        await _feed(processor, [7, 0, 0])
    if outcome == "busy":
        _stage(processor, "waiting")
    elif outcome == "rejected":
        builder.side_effect = ValueError("invalid replacement")
    _stage(processor)
    if outcome == "cancelled":
        _cancel(processor)
    original, = [result for result in _results(events, EventType.StageProcessingConfiguration.name)
                 if result.command_id == "update"]
    calls = builder.call_count
    _stage(processor, settings_snapshot={"CONFIG.revision": "different"})
    conflict = _results(events)[-1]
    assert not conflict.ok
    assert conflict.error_code == "PROCESSING_CONFIGURATION_CONFLICT"
    assert conflict.data is None
    _stage(processor)
    assert _results(events)[-1] == original
    assert builder.call_count == calls
    if outcome == "busy":
        await _feed(processor, [0, 255, 9, 0], 2)
        assert processor.config_revision == "new"
        assert _results(events)[-1].command_id == "waiting"
        _stage(processor)
        assert _results(events)[-1] == original


def test_retry_ignores_dictionary_key_order(configured):
    processor, events, _, builder = configured
    _stage(processor)
    original, = _results(events)
    processor._execute((module.SignalProcessorCommand.stage_configuration, CommandRequest("update", {
        "generation": "run", "expected_revision": "old",
        "settings_snapshot": {"CONFIG.generation": "run", "CONFIG.revision": "new"},
    })))
    assert _results(events) == [original, original]
    builder.assert_called_once()


@pytest.mark.parametrize("invalid", [object(), float("nan")], ids=["not-json", "nan"])
def test_unfingerprintable_request_does_not_reserve_command_id(configured, invalid):
    processor, events, _, builder = configured
    _stage(processor, settings_snapshot={"invalid": invalid})
    rejected, = _results(events)
    assert rejected.error_code == "PROCESSING_CONFIGURATION_REJECTED"
    _stage(processor)
    assert _results(events)[-1].ok
    assert processor.config_revision == "new"
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_unfingerprintable_duplicate_preserves_original_command(configured, pending):
    processor, events, _, builder = configured
    if pending:
        await _feed(processor, [7, 0, 0])
    _stage(processor)
    _stage(processor, settings_snapshot={"invalid": float("nan")})
    assert _results(events)[-1].error_code == "PROCESSING_CONFIGURATION_REJECTED"
    if pending:
        await _feed(processor, [0, 255, 9, 0], 2)
    original = next(result for result in _results(events) if result.ok)
    _stage(processor)
    assert _results(events)[-1] == original
    assert processor.config_revision == "new"
    builder.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_cancel_before_boundary_keeps_old_configuration(configured):
    processor, events, _, _ = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _cancel(processor)
    stage, cancellation = _results(events)
    assert not stage.ok and stage.data["status"] == "cancelled"
    assert cancellation.ok and cancellation.data["applied_revision"] == "old"
    await _feed(processor, [0, 255, 9, 0], 2)
    assert processor.config_revision == "old"


def test_timeout_cancel_after_install_reports_applied_and_never_rolls_back(configured):
    processor, events, _, _ = configured
    _stage(processor)
    _cancel(processor)
    cancellation = _results(events)[-1]
    assert cancellation.ok
    assert cancellation.data["status"] == "applied"
    assert cancellation.data["applied_revision"] == "new"
    assert processor.config_revision == "new"


@pytest.mark.parametrize("snapshot", [
    {"CONFIG.revision": "new", "CONFIG.generation": "run"},
    {"CONFIG.revision": "different"},
    {"invalid": float("nan")},
])
def test_cancel_tombstone_blocks_a_later_stage_with_same_id(configured, snapshot):
    processor, events, _, builder = configured
    _cancel(processor)
    cancelled = _results(events)[0]
    _stage(processor, settings_snapshot=snapshot)
    assert processor.config_revision == "old"
    assert _results(events)[-1] == cancelled
    builder.assert_not_called()


@pytest.mark.asyncio
async def test_early_cancellation_preserves_another_pending_update(configured):
    processor, events, _, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    _cancel(processor, target="delayed")
    cancelled, acknowledgement = _results(events)
    assert cancelled.command_id == "delayed"
    assert cancelled.data["status"] == "cancelled"
    assert acknowledgement.ok
    await _feed(processor, [0, 255, 9, 0], 2)
    assert processor.config_revision == "new"
    assert _results(events)[-1].command_id == "update"
    assert _results(events)[-1].data["status"] == "applied"
    _stage(processor, "delayed")
    assert _results(events)[-1] == cancelled
    builder.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [module.SignalProcessorCommand.should_stop,
                                    module.SignalProcessorCommand.end_session])
async def test_stop_or_session_end_returns_cancelled_receipt_for_pending_update(configured, command):
    processor, events, _, _ = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor)
    processor._execute((command, processor.session_runtime.session.session_id))
    result, = _results(events)
    assert not result.ok
    assert result.data["status"] == "cancelled"
    assert processor._configuration_transactions.pending is None
    assert processor.config_revision == "old"


def test_restart_generation_rejects_old_commands_and_pins_new_epochs(configured):
    processor, events, _, builder = configured
    processor.generation = "restarted"
    processor.config_revision = "restart-revision"
    _stage(processor)
    assert not _results(events)[0].ok
    builder.assert_not_called()
    assert processor.new_epoch_processor().config_revision == "restart-revision"
