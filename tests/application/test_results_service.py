"""Decoded results need no experiment lifecycle and stay within one engine run."""
from types import SimpleNamespace

from omnibci.application.events import TrialOutcome
from omnibci.application.services.results_service import ResultsService
from omnibci.domain.classification import Classification
from omnibci.shared.events import Event, EventType


def publish(service, generation, number, *, revision="revision"):
    service.on_trial_completed(Event(EventType.TrialCompleted, TrialOutcome(
        generation, number, Classification(target_id=3, event_id=8), revision,
    )))


def test_first_decoded_trial_is_available_without_starting_a_session():
    service = ResultsService(SimpleNamespace(generation="engine-1"))
    publish(service, "engine-1", 1)

    snapshot = service.snapshot()
    assert snapshot["generation"] == "engine-1"
    assert [(item["trial_number"], item["event_id"], item["target_id"])
            for item in snapshot["trials"]] == [(1, 8, 3)]
    assert snapshot["trials"][0]["config_revision"] == "revision"


def test_duplicate_delivery_and_configuration_changes_do_not_reset_trials():
    service = ResultsService(SimpleNamespace(generation="engine-1"))
    publish(service, "engine-1", 1)
    publish(service, "engine-1", 1)
    publish(service, "engine-1", 2, revision="next-revision")

    assert [(item["trial_number"], item["config_revision"])
            for item in service.snapshot()["trials"]] == [(1, "revision"), (2, "next-revision")]


def test_restart_discards_previous_generation_and_late_events():
    engine = SimpleNamespace(generation=None)
    service = ResultsService(engine)
    assert service.snapshot() == {"generation": None, "trials": []}
    engine.generation = "engine-1"
    publish(service, "engine-1", 1)
    engine.generation = "engine-2"
    publish(service, "engine-1", 2)
    assert service.snapshot() == {"generation": "engine-2", "trials": []}
    publish(service, "engine-2", 1)
    publish(service, "engine-1", 3)
    assert [item["generation"] for item in service.snapshot()["trials"]] == ["engine-2"]


def test_result_history_is_bounded_and_snapshots_are_independent():
    service = ResultsService(SimpleNamespace(generation="engine-1"))
    for number in range(1, 206):
        publish(service, "engine-1", number)
    snapshot = service.snapshot()
    assert [item["trial_number"] for item in snapshot["trials"]] == list(range(6, 206))
    snapshot["trials"][0]["target_id"] = 99
    snapshot["trials"].clear()
    assert len(service.snapshot()["trials"]) == 200
    assert service.snapshot()["trials"][0]["target_id"] == 3


def test_generation_is_checked_after_entering_the_result_lock():
    engine = SimpleNamespace(generation="old")
    service = ResultsService(engine)

    class RestartBeforeLock:
        def __enter__(self):
            engine.generation = "new"

        def __exit__(self, *args):
            pass

    service._lock = RestartBeforeLock()
    publish(service, "old", 1)
    assert service.snapshot() == {"generation": "new", "trials": []}
