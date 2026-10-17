"""A repeated delivery cannot duplicate or overwrite persisted trial results."""

import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.shared.events import Event, EventType


def test_identical_trial_delivery_is_idempotent_even_after_session_end(session_service):
    sid = session_service.handle_create_session(CreateSessionCommand())
    event = Event(EventType.TrialCompleted, TrialOutcome(sid, 1, Classification(2, 7)))
    session_service.on_trial_completed(event)
    session_service.handle_end_session(EndSessionCommand(sid))
    session_service.on_trial_completed(event)
    assert session_service._repo.find_by_id(sid).trial_count == 1


def test_conflicting_trial_delivery_does_not_replace_history(session_service):
    sid = session_service.handle_create_session(CreateSessionCommand())
    original = Event(EventType.TrialCompleted, TrialOutcome(sid, 1, Classification(2, 7)))
    session_service.on_trial_completed(original)
    with pytest.raises(ValueError, match="Conflicting result"):
        session_service.on_trial_completed(Event(
            EventType.TrialCompleted, TrialOutcome(sid, 1, Classification(3, 7)),
        ))
    assert session_service._repo.find_by_id(sid).trials[0].target_id == 2
