"""One processing owner prepares, replaces and releases decoder resources."""
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnibci.application.events import CommandFailureKind, CommandRequest
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.infrastructure.signal_processing.pipeline_configuration import ConfiguredPipeline
from omnibci.infrastructure.signal_processing import configuration_transactions


class Decoder:
    def __init__(self, name, calls, *, fail_setup=False, fail_close=False):
        self.name, self.calls = name, calls
        self.fail_setup, self.fail_close = fail_setup, fail_close

    def setup(self):
        self.calls.append((self.name, "setup"))
        if self.fail_setup:
            raise ValueError("model setup failed")

    def begin_epoch(self):
        self.calls.append((self.name, "begin"))

    def teardown(self):
        self.calls.append((self.name, "close"))
        if self.fail_close:
            raise RuntimeError("model cleanup failed")


def prepared(monkeypatch, *, active=True, fail_setup=False, fail_close=False):
    calls, events = [], []
    old = Decoder("old", calls)
    new = Decoder("new", calls, fail_setup=fail_setup, fail_close=fail_close)
    snapshot = {"CONFIG.generation": "run", "CONFIG.engine_revision": "engine",
                "CONFIG.module_revisions": {"processing": "old", "acquisition": "source"}}
    owner = SignalProcessor(None, old, Queue(), MagicMock(), SimpleNamespace(put_event=events.append),
                            sample_rate=250, settings_snapshot=snapshot)
    candidate = ConfiguredPipeline(new, (), {**snapshot, "CONFIG.module_revisions": {"processing": "new", "acquisition": "source"}})
    monkeypatch.setattr(configuration_transactions, "build_pipeline_update", lambda *args, **kwargs: candidate)
    if active:
        owner.current_epoch = object()
    request = CommandRequest("stage", {"generation": "run", "engine_revision": "engine", "expected_revision": "old",
                                       "boundary": "trial", "settings_snapshot": candidate.settings_snapshot})
    return owner, old, new, calls, events, request


def test_staging_prepares_once_without_replacing_active_epoch(monkeypatch):
    owner, old, new, calls, events, request = prepared(monkeypatch)
    owner._stage_configuration(request)
    owner._stage_configuration(request)
    assert owner._decoder is old and not events
    assert calls == [("old", "setup"), ("new", "setup")]
    owner.transition_to_awaiting()
    assert owner._decoder is new and owner.processing_revision == "new"
    assert calls[-1] == ("old", "close") and events[-1].data.ok


def test_candidate_setup_failure_cleans_partial_resources_and_keeps_active_model(monkeypatch):
    owner, old, _, calls, events, request = prepared(monkeypatch, fail_setup=True)
    owner._stage_configuration(request)
    assert owner._decoder is old and owner.processing_revision == "old"
    assert owner._configuration_transactions.pending is None
    assert calls == [("old", "setup"), ("new", "setup"), ("new", "close")]
    assert not events[-1].data.ok
    owner._stage_configuration(request)
    assert calls.count(("new", "setup")) == 1


def test_cancellation_releases_candidate_and_preserves_old_model(monkeypatch):
    owner, old, _, calls, events, request = prepared(monkeypatch)
    owner._stage_configuration(request)
    cancel = CommandRequest("cancel", {"command_id": "stage", "generation": "run", "engine_revision": "engine"})
    owner._cancel_configuration(cancel)
    owner._cancel_configuration(cancel)
    assert owner._decoder is old and owner.processing_revision == "old"
    assert calls.count(("new", "close")) == 1 and ("old", "close") not in calls
    assert events[-1].data.ok


def test_input_reset_cancels_candidate_before_transition_and_releases_old_binding(monkeypatch):
    owner, _, _, calls, events, request = prepared(monkeypatch)
    owner._stage_configuration(request)
    owner._reset_input(CommandRequest("reset", {"generation": "run", "engine_revision": "engine", "expected_revision": "old"}))
    assert owner.processing_revision == "old" and owner._decoder is None and owner.detector is None
    assert calls.count(("new", "close")) == calls.count(("old", "close")) == 1
    assert all(not event.data.ok for event in events if event.data.command_id == "stage")
    assert events[-1].data.ok


@pytest.mark.asyncio
async def test_stop_releases_candidate_and_active_model_once(monkeypatch):
    owner, _, _, calls, _, request = prepared(monkeypatch)
    owner._stage_configuration(request)
    owner._execute((SignalProcessorCommand.should_stop, None))
    await owner.serve()
    assert calls.count(("old", "close")) == calls.count(("new", "close")) == 1


def test_failed_candidate_cleanup_does_not_report_success_or_allow_another_model(monkeypatch):
    owner, _, _, calls, events, request = prepared(monkeypatch, fail_close=True)
    owner._stage_configuration(request)
    owner._cancel_configuration(CommandRequest("cancel", {"command_id": "stage", "generation": "run", "engine_revision": "engine"}))
    assert not events[-1].data.ok
    owner._stage_configuration(CommandRequest("another", request.payload))
    assert calls.count(("new", "setup")) == 1
    assert not events[-1].data.ok


def test_constructor_setup_failure_releases_partial_model():
    calls = []
    decoder = Decoder("initial", calls, fail_setup=True)
    with pytest.raises(ValueError, match="model setup failed"):
        SignalProcessor(None, decoder, Queue(), MagicMock(), MagicMock())
    assert calls == [("initial", "setup"), ("initial", "close")]


def test_replacement_cleanup_failure_rejects_candidate_and_stops_processing(monkeypatch):
    owner, old, _, calls, events, request = prepared(monkeypatch, active=False)
    old.fail_close = True
    owner._stage_configuration(request)
    assert owner._decoder is old and owner.processing_revision == "old"
    assert owner._should_stop and owner._configuration_transactions.pending is None
    assert calls == [("old", "setup"), ("new", "setup"), ("old", "close"), ("new", "close")]
    assert not events[-1].data.ok
    assert events[-1].data.error_code == "PROCESSING_CLEANUP_FAILED"
    assert events[-1].data.failure_kind is CommandFailureKind.EXECUTION_FAILED


@pytest.mark.asyncio
async def test_exception_exit_cleans_pending_and_active_models(monkeypatch):
    owner, _, _, calls, _, request = prepared(monkeypatch)
    owner._stage_configuration(request)
    async def fail():
        raise ValueError("processing failed")
    owner._current_state = SimpleNamespace(run=fail)
    with pytest.raises(ValueError, match="processing failed"):
        await owner.serve()
    assert calls.count(("old", "close")) == calls.count(("new", "close")) == 1


@pytest.mark.asyncio
async def test_failed_teardown_is_not_retried_and_other_resources_still_close(monkeypatch):
    owner, _, _, calls, events, request = prepared(monkeypatch, fail_close=True)
    owner._stage_configuration(request)
    owner._execute((SignalProcessorCommand.should_stop, CommandRequest("stop")))
    assert not events[-1].data.ok
    with pytest.raises(RuntimeError, match="cleanup failed"):
        await owner.serve()
    assert calls.count(("old", "close")) == calls.count(("new", "close")) == 1


def test_failed_setup_and_cleanup_prevents_new_binding_and_successful_reset(monkeypatch):
    owner, _, _, calls, events, request = prepared(monkeypatch, fail_setup=True, fail_close=True)
    with pytest.raises(RuntimeError, match="cleanup failed"):
        owner._stage_configuration(request)
    assert not events[-1].data.ok and owner._should_stop
    owner._reset_input(CommandRequest("reset", {"generation": "run", "engine_revision": "engine", "expected_revision": "old"}))
    assert not events[-1].data.ok
    owner._stage_configuration(CommandRequest("another", request.payload))
    assert calls.count(("new", "setup")) == 1 and not events[-1].data.ok
