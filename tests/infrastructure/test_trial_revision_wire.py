"""Trial configuration identity survives local and network result publication."""

from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.infrastructure.outbound import event_to_wire, payload_to_jsonable
from omnibci.presentation.schemas.ws import TrialCompletedMessage
from omnibci.shared.events import Event, EventType


def test_trial_revision_survives_websocket_and_peer_wire_contracts():
    outcome = TrialOutcome("generation", 2, Classification(target_id=1, event_id=1), "revision-2")
    wire = event_to_wire(Event(EventType.TrialCompleted, outcome))
    assert wire["config_revision"] == "revision-2"
    assert TrialCompletedMessage.model_validate(wire).config_revision == "revision-2"
    assert payload_to_jsonable(outcome)["config_revision"] == "revision-2"
