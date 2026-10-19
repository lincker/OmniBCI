"""Session ownership and result publication remain outside signal processing."""

from unittest.mock import Mock

import pytest

from omnibci.application.processing_session import DecodedTrial
from omnibci.application.session_runtime import SessionRuntime
from omnibci.domain.classification import Classification
from omnibci.domain.epoch import EpochInfo
from omnibci.domain.events import SessionEnded, SessionStarted, TrialCompleted


def result(event_id=7):
    return DecodedTrial(EpochInfo(event_id, 2, 250, 20, 3),
                        Classification(target_id=1, event_id=event_id), "epoch-revision")


def test_recording_retains_only_epoch_metadata_and_publishes_before_drain_receipt():
    published = []
    runtime = SessionRuntime(published.extend)
    runtime.start_session({"session_id": "session"}, config_revision="session-revision", before_end=lambda _: None)

    decoded = result()
    completed = runtime.record_trial(decoded)
    receipt = runtime.drain_receipt(generation="run", config_revision="current",
                                    last_sequence=3, interrupted_epoch=False)

    assert runtime.session.trials[0].epoch_info is decoded.epoch_info
    assert runtime.session.trials[0].config_revision == "epoch-revision"
    assert published[-1] is completed
    assert isinstance(completed, TrialCompleted)
    assert receipt.session_id == "session"
    assert receipt.completed_trials == 1
    assert runtime.session.pop_events() == []


def test_standby_result_is_dropped_without_publication():
    publish = Mock()
    runtime = SessionRuntime(publish)
    assert runtime.record_trial(result()) is None
    assert runtime.session.trial_count == 0
    publish.assert_not_called()


def test_replacement_cancels_pending_configuration_before_end_and_start_events():
    operations = []
    runtime = SessionRuntime(operations.extend)
    runtime.start_session({"session_id": "old"}, config_revision="revision", before_end=lambda _: None)
    operations.clear()
    runtime.start_session({"session_id": "new"}, config_revision="revision",
                          before_end=lambda message: operations.append("cancel_pending"))
    assert operations[0] == "cancel_pending"
    assert isinstance(operations[1], SessionEnded)
    assert operations[1].session_id == "old"
    assert isinstance(operations[2], SessionStarted)
    assert operations[2].session_id == "new"


def test_old_session_drain_does_not_certify_new_session():
    runtime = SessionRuntime(lambda _: None)
    runtime.start_session({"session_id": "new"}, config_revision=None, before_end=lambda _: None)
    with pytest.raises(ValueError, match="session differs"):
        runtime.validate_drain("old")
