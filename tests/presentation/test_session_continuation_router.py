"""Session queries expose continuation links and the revision used by each trial."""
from omnibci.application.commands.session_commands import CreateSessionCommand, EndSessionCommand
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.shared.events import Event, EventType


def test_rest_round_trip_preserves_session_and_trial_metadata(api_client, session_service):
    previous = session_service.handle_create_session(CreateSessionCommand(config_revision="old"))
    session_service.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome(
        session_id=previous, trial_number=1,
        classification=Classification(target_id=2, event_id=7), config_revision="old",
    )))
    session_service.handle_end_session(EndSessionCommand(previous, "configuration_apply"))
    continued = session_service.create_continuation(previous, config_revision="new")

    old = api_client.get(f"/api/v1/sessions/{previous}").json()["data"]
    new = api_client.get(f"/api/v1/sessions/{continued}").json()["data"]
    active = api_client.get("/api/v1/sessions/active").json()["data"]
    sessions = api_client.get("/api/v1/sessions").json()["data"]["sessions"]
    trial = api_client.get(f"/api/v1/sessions/{previous}/trials").json()["data"][0]
    last = api_client.get(f"/api/v1/sessions/{previous}/trials/last").json()["data"]

    assert old["config_revision"] == "old"
    assert old["interruption_reason"] == "configuration_apply"
    assert old["continuation_of"] is None
    assert new["continuation_of"] == previous
    assert new["config_revision"] == "new"
    assert new == active
    assert {item["session_id"]: item for item in sessions} == {previous: old, continued: new}
    assert trial["config_revision"] == last["config_revision"] == "old"
