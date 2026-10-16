"""Worker session restarts retain metadata and never reopen a closed identity."""
from unittest.mock import MagicMock

import numpy as np

from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.domain.session import SessionState
from omnibci.infrastructure.neuroengine.signal_processing.signal_processor import SignalProcessor
from omnibci.shared.events import EventType


def processor():
    return SignalProcessor(
        detector=MagicMock(), decoder=MagicMock(), command_queue=MagicMock(),
        data_manager=MagicMock(), event_bus=MagicMock(), auto_start_session=False,
        config_revision="pinned",
    )


def test_start_end_and_continuation_metadata_reach_aggregate():
    worker = processor()
    assert worker._start_session({"session_id": "first"})
    assert worker.session.config_revision == "pinned"
    assert worker._end_session({"session_id": "first", "interruption_reason": "configuration_apply"})
    ended = worker.event_bus.put_event.call_args.args[0]
    assert ended.type == EventType.SessionEnded
    assert ended.data.interruption_reason == "configuration_apply"
    assert worker._start_session({
        "session_id": "second", "continuation_of": "first", "config_revision": "new",
    })
    assert worker.session.continuation_of == "first"
    assert worker.session.config_revision == "new"


def test_duplicate_start_preserves_trials_and_emits_no_new_session_events():
    worker = processor()
    payload = {"session_id": "first", "paradigm": "classic40SSVEP", "config_revision": "pinned"}
    assert worker._start_session(payload)
    session = worker.session
    session.record_trial(
        Epoch(data=np.ones((2, 4)), event_id=1, sample_rate=1000, channel_count=2),
        Classification(target_id=1, event_id=1),
    )
    worker.event_bus.reset_mock()
    assert worker._start_session(payload)
    assert worker.session is session
    assert worker.session.trial_count == 1
    worker.event_bus.put_event.assert_not_called()
    assert not worker._start_session({**payload, "config_revision": "different"})
    assert worker.session is session


def test_closed_ids_cannot_reopen_or_close_a_later_running_session():
    worker = processor()
    assert worker._start_session({"session_id": "first"})
    assert worker._end_session("first")
    assert not worker._start_session({"session_id": "first"})
    assert worker.session.state is SessionState.ENDED
    assert worker._start_session({"session_id": "second"})
    assert not worker._start_session({"session_id": "first"})
    assert worker.session.session_id == "second"
    assert worker.session.state is SessionState.RUNNING
