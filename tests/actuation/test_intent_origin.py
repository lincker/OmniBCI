"""Trial identity survives every terminal output-command outcome."""
from copy import deepcopy
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnibci.application.commands.actuation_commands import InvalidActuationCommand, validate_actuation_command
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.outbound import payload_to_jsonable
from omnibci.shared.events import Event, EventType
from tests.actuation.test_application import _app, _run
from tests.actuation_helpers import actuation_payload


def test_translator_preserves_trial_origin_without_giving_it_to_the_device():
    bus = MagicMock()
    translator = ActuationCommandTranslator(bus)
    outcome = TrialOutcome("run", 17, Classification(3, 4, 0.25, forced=True), "p", "a")
    translator.on_trial_completed(Event(EventType.TrialCompleted, outcome))
    intent = bus.put_event.call_args.args[0].data
    assert type(intent).__name__ == "ActuationIntent"
    wire = payload_to_jsonable(intent)
    assert wire["origin"] == {
        "generation": "run", "trial_number": 17, "processing_revision": "p",
        "acquisition_revision": "a", "forced": True,
        "produced_at": outcome.classification.produced_at.isoformat(),
    }
    assert wire["parameters"] == {"target_id": 3, "event_id": 4, "confidence": 0.25}
    translator.on_trial_completed(Event(EventType.TrialCompleted, outcome))
    assert payload_to_jsonable(bus.put_event.call_args.args[0].data)["command_id"] != wire["command_id"]
    assert validate_actuation_command(wire).origin == wire["origin"]


@pytest.mark.parametrize("value", [Classification(1, 1), SimpleNamespace(target_id=True),
                                    SimpleNamespace(target_id=1.9)])
def test_translator_rejects_noncanonical_trial(value):
    bus = MagicMock()
    ActuationCommandTranslator(bus).on_trial_completed(Event(EventType.TrialCompleted, value))
    bus.put_event.assert_not_called()


@pytest.mark.parametrize("status", ["ok", "superseded", "stale", "cancelled", "fault"])
def test_every_terminal_outcome_retains_command_and_origin(status):
    app = _app(fail_on={1} if status == "fault" else frozenset())
    first = actuation_payload(command_id="first", issued_at=1.0)
    if status == "cancelled":
        app._paused = True
    if status == "stale":
        app._max_command_age = 1.0
    app._enqueue_command(Event(EventType.ActuationCommand, first))
    if status == "superseded":
        app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload(command_id="second")))
    _run(app.execute_command())
    result = app.event_bus.events[0]
    assert result.type is (EventType.ActuationFault if status == "fault" else EventType.ActuationAck)
    assert result.data["command_id"] == "first"
    assert result.data["origin"] == first["origin"]
    if status == "fault":
        assert result.data["code"] == "COMMAND_FAILED"
    else:
        assert result.data["status"] == status
    if status == "ok":
        assert app.actuator.executed == [{"target_id": 1}]


def test_command_result_identifies_the_actual_output_host_operation():
    app = _app()
    app._runtime_snapshot = {"CONFIG.generation": "host-run", "CONFIG.operation_id": "host-prepare",
                             "CONFIG.module_revisions": {"actuation": "a" * 64}}
    app._enqueue_command(Event(EventType.ActuationCommand, actuation_payload()))
    _run(app.execute_command())
    assert app.event_bus.events[0].data["execution"] == {
        "module": "actuation", "generation": "host-run", "operation_id": "host-prepare",
        "module_revision": "a" * 64,
    }


@pytest.mark.parametrize("change", [
    {"command_id": ""}, {"command_id": True}, {"issued_at": float("nan")},
    {"origin": {}}, {"parameters": {"target_id": 1.5}},
    {"parameters": {"target_id": True}}, {"seq": 1},
])
def test_closed_envelope_rejects_invalid_identity_and_target(change):
    payload = actuation_payload()
    payload.update(change)
    with pytest.raises(InvalidActuationCommand):
        validate_actuation_command(payload)


def test_origin_and_device_parameters_are_independent_snapshots():
    payload = actuation_payload({"target_id": 1, "options": {"axis": "left"}})
    original = deepcopy(payload)
    command = validate_actuation_command(payload)
    payload["origin"]["generation"] = "wrong"
    payload["parameters"]["options"]["axis"] = "right"
    assert command.origin == original["origin"]
    assert command.parameters == original["parameters"]
    command.origin["generation"] = "discarded"
    command.parameters["options"]["axis"] = "discarded"
    assert command.to_dict() == original


def test_command_dispatch_keeps_the_actuation_envelope_separate_from_command_request():
    from omnibci.application.command_contracts import command_event
    payload = actuation_payload()
    event = command_event(Event(EventType.ActuationCommand, payload))
    assert event.data.to_dict() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_cancelled_owner_waits_and_reports_the_actual_device_outcome(fails):
    app = _app()
    started, release = threading.Event(), threading.Event()
    def execute(command):
        started.set()
        assert release.wait(5)
        if fails:
            raise RuntimeError("device failed")
    app.actuator.execute = execute
    payload = actuation_payload()
    app._enqueue_command(Event(EventType.ActuationCommand, payload))
    task = asyncio.create_task(app.execute_command())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not app.event_bus.events
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        if app._device_executor is not None:
            app._device_executor.shutdown(wait=True)
    result, = app.event_bus.events
    assert result.data["command_id"] == payload["command_id"]
    assert result.data["origin"] == payload["origin"]
    if fails:
        assert result.type is EventType.ActuationFault and result.data["code"] == "COMMAND_FAILED"
    else:
        assert result.type is EventType.ActuationAck and result.data["status"] == "ok"
