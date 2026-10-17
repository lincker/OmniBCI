"""Subject references remain optional opaque identifiers on a session."""

from datetime import datetime, timezone

import pytest

from omnibci.domain.repositories import SessionRecord, SessionRecordState
from omnibci.domain.session import BCISession


def _record(subject_id):
    return SessionRecord(
        session_id="session", paradigm=None, state=SessionRecordState.RUNNING,
        started_at=datetime.now(timezone.utc), subject_id=subject_id,
    )


@pytest.mark.parametrize("subject_id", [None, "subject-123", " participant / external "])
def test_subject_identity_is_preserved_without_normalization(subject_id):
    session = BCISession(subject_id=subject_id)
    session.start()

    assert session.subject_id == subject_id
    assert session.pop_events()[0].subject_id == subject_id
    assert _record(subject_id).subject_id == subject_id


@pytest.mark.parametrize("subject_id", ["", " \t\n", 123, True, [], {}])
@pytest.mark.parametrize("factory", [BCISession, _record])
def test_invalid_subject_identity_is_rejected(factory, subject_id):
    with pytest.raises(ValueError, match="subject_id"):
        factory(subject_id=subject_id)
