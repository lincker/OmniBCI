"""Decoded results need no experiment lifecycle and stay within one engine run."""
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from omnibci.application.events import EpochProcessed, TrialOutcome
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


def publish_epoch(service, number, *, generation="run", status="no_result", reason="next_start"):
    classified = status == "decoded"
    record = EpochProcessed(
        generation=generation, epoch_number=number, event_id=8,
        config_revision="revision", sample_rate=1000, channel_count=9,
        sample_count=3000, pre_trigger_samples=200,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        completed_at=datetime(2026, 1, 1, 0, 0, 3, tzinfo=timezone.utc),
        status=status, reason=reason,
        classification=Classification(3, 8, confidence=0.9) if classified else None,
        trial_number=1 if classified else None,
        error="Decoder failed" if status == "error" else None,
    )
    service.on_epoch_processed(Event(EventType.EpochProcessed, record))


def test_every_terminal_epoch_is_listed_without_requiring_a_prediction():
    service = ResultsService(SimpleNamespace(generation="run"))
    for number, (status, reason) in enumerate([
        ("decoded", "prediction"), ("no_result", "next_start"),
        ("interrupted", "stream_stop"), ("error", "error"),
    ], 1):
        publish_epoch(service, number, status=status, reason=reason)

    page = service.epochs()
    assert page["total"] == 4
    assert [item["status"] for item in page["epochs"]] == ["error", "interrupted", "no_result", "decoded"]
    assert [item["epoch_number"] for item in page["epochs"]] == [4, 3, 2, 1]
    assert page["epochs"][0]["error"] == "Decoder failed"
    assert page["epochs"][1]["result"] is None
    assert page["epochs"][-1]["result"] == {"target_id": 3, "confidence": 0.9, "forced": False}
    assert page["epochs"][-1]["sample_count"] == 3000
    assert service.snapshot()["trials"] == []


def test_epoch_history_retains_entire_run_and_supports_independent_pages():
    service = ResultsService(SimpleNamespace(generation="run"))
    for number in range(1, 206):
        publish_epoch(service, number)
    publish_epoch(service, 205)

    page = service.epochs(offset=200, limit=25)
    assert page["total"] == 205
    assert [item["epoch_number"] for item in page["epochs"]] == [5, 4, 3, 2, 1]
    page["epochs"][0]["status"] = "error"
    assert service.epochs(offset=200)["epochs"][0]["status"] == "no_result"
    assert service.epochs(offset=205)["epochs"] == []


def test_epoch_history_resets_on_new_generation_and_rejects_late_records():
    engine = SimpleNamespace(generation="run")
    service = ResultsService(engine)
    publish_epoch(service, 1)
    engine.generation = "new"
    publish_epoch(service, 2)
    assert service.epochs() == {"generation": "new", "total": 0, "offset": 0, "limit": 25, "epochs": []}
    publish_epoch(service, 1, generation="new")
    assert service.epochs()["total"] == 1
    assert service.epochs()["epochs"][0]["generation"] == "new"


@pytest.mark.parametrize("arguments", [{"offset": -1}, {"limit": 0}, {"limit": 101}, {"offset": True}])
def test_epoch_page_rejects_invalid_bounds(arguments):
    with pytest.raises(ValueError, match="epoch page"):
        ResultsService(SimpleNamespace(generation=None)).epochs(**arguments)
