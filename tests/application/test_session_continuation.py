"""A reconfigured recording continues under a new identity with revision history."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.events import SessionSummary, TrialOutcome
from omnibci.application.queries.session_queries import GetSessionQuery, GetSessionTrialsQuery
from omnibci.application.services.session_service import SessionAppService
from omnibci.domain.classification import Classification
from omnibci.domain.epoch import Epoch
from omnibci.domain.session import BCISession
from omnibci.infrastructure.event_translator import DomainEventPublisher
from omnibci.shared.events import Event, EventType


def test_continuation_uses_new_id_and_keeps_previous_trials(session_service):
    previous = session_service.handle_create_session(CreateSessionCommand(
        paradigm="classic40SSVEP", config_revision="revision-old",
    ))
    session_service.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome(
        session_id=previous, trial_number=1,
        classification=Classification(target_id=2, event_id=7),
        config_revision="revision-old",
    )))
    session_service.handle_end_session(EndSessionCommand(previous, "configuration_apply"))
    continued = session_service.create_continuation(previous, config_revision="revision-new")

    old = session_service.handle_get_session(GetSessionQuery(previous))
    new = session_service.handle_get_session(GetSessionQuery(continued))
    assert continued != previous
    assert old.state == "ended"
    assert old.trial_count == 1
    assert old.config_revision == "revision-old"
    assert old.interruption_reason == "configuration_apply"
    assert new.continuation_of == previous
    assert new.config_revision == "revision-new"
    assert new.paradigm == "classic40SSVEP"
    assert new.trial_count == 0
    assert session_service.active_session_id == continued
    assert session_service.handle_get_trials(GetSessionTrialsQuery(previous))[0].config_revision == "revision-old"


def test_continuation_requires_closed_previous_and_no_other_active_session(session_service):
    with pytest.raises(KeyError):
        session_service.create_continuation("unknown")
    previous = session_service.handle_create_session(CreateSessionCommand())
    with pytest.raises(RuntimeError, match="ended previous"):
        session_service.create_continuation(previous)
    session_service.handle_end_session(EndSessionCommand(previous))
    session_service.handle_create_session(CreateSessionCommand())
    with pytest.raises(RuntimeError, match="another session"):
        session_service.create_continuation(previous)


def test_commands_propagate_continuation_revision_and_interruption(session_repository):
    engine = SimpleNamespace(applied_snapshot={"CONFIG.revision": "applied-revision"})
    broker = Mock()
    service = SessionAppService(session_repository, engine, broker)
    previous = service.handle_create_session(CreateSessionCommand())
    assert broker.request.call_args.args == (EventType.StartSession, {
        "session_id": previous, "paradigm": None, "config_revision": "applied-revision",
    })
    service.handle_end_session(EndSessionCommand(previous, "configuration_apply"))
    assert broker.request.call_args.args == (EventType.EndSession, {
        "session_id": previous, "interruption_reason": "configuration_apply",
    })
    continued = service.create_continuation(previous, config_revision="new", paradigm="new-paradigm")
    assert broker.request.call_args.args == (EventType.StartSession, {
        "session_id": continued, "paradigm": "new-paradigm",
        "continuation_of": previous, "config_revision": "new",
    })


def test_failed_continuation_preserves_previous_record(session_repository):
    broker = Mock()
    service = SessionAppService(session_repository, command_results=broker)
    previous = service.handle_create_session(CreateSessionCommand())
    service.handle_end_session(EndSessionCommand(previous, "configuration_apply"))
    broker.request.side_effect = RuntimeError("worker rejected session")
    with pytest.raises(RuntimeError, match="worker rejected"):
        service.create_continuation(previous, config_revision="new")
    assert len(session_repository.find_all()) == 1
    assert session_repository.find_by_id(previous).state == "ended"
    assert service.active_session_id is None


def test_domain_events_and_projection_keep_trial_revision_and_end_reason(session_service):
    session_id = session_service.handle_create_session(CreateSessionCommand(config_revision="first"))
    aggregate = BCISession(session_id=session_id, continuation_of="previous", config_revision="first")
    aggregate.start()
    started = aggregate.pop_events()[0]
    assert started.continuation_of == "previous"
    assert started.config_revision == "first"
    epoch = Epoch(data=np.ones((2, 4)), event_id=7, sample_rate=1000, channel_count=2)
    classification = Classification(target_id=2, event_id=7)
    aggregate.record_trial(epoch, classification)
    aggregate.record_trial(epoch, classification, config_revision="second")
    assert [trial.config_revision for trial in aggregate.trials] == ["first", "second"]

    bus = Mock()
    publisher = DomainEventPublisher(bus)
    publisher.publish_all(aggregate.pop_events())
    for call in bus.put_event.call_args_list:
        session_service.on_trial_completed(call.args[0])
    assert [trial.config_revision for trial in session_service.handle_get_trials(
        GetSessionTrialsQuery(session_id),
    )] == ["first", "second"]

    aggregate.end(interruption_reason="configuration_apply")
    publisher.publish_all(aggregate.pop_events())
    summary = bus.put_event.call_args.args[0]
    assert summary.data.config_revision == "first"
    assert summary.data.interruption_reason == "configuration_apply"
    session_service.on_session_ended(summary)
    session_service.on_session_ended(Event(EventType.SessionEnded, SessionSummary(session_id, 2)))
    dto = session_service.handle_get_session(GetSessionQuery(session_id))
    assert dto.interruption_reason == "configuration_apply"
    with pytest.raises(RuntimeError):
        aggregate.start()


def test_continuation_cannot_reuse_its_parent_identity():
    with pytest.raises(ValueError, match="new session ID"):
        BCISession(session_id="old", continuation_of="old")
