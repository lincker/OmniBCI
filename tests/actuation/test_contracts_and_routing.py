"""Actuation command envelope validation, trial origin preservation, and peer route symmetry."""
import asyncio
from collections import UserDict
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnibci.application.commands.actuation_commands import InvalidActuationCommand, validate_actuation_command
from omnibci.application.events import TrialOutcome
from omnibci.domain.classification import Classification
from omnibci.infrastructure.actuation.engine_channel import engine_peer_config
from omnibci.infrastructure.neuroengine.actuation_translator import ActuationCommandTranslator
from omnibci.infrastructure.neuroengine.gateway.peers import actuation_peer_config
from omnibci.infrastructure.outbound import payload_to_jsonable
from omnibci.shared.events import Event, EventType
from tests.actuation.test_application import _app, _run
from tests.actuation_helpers import actuation_payload


class TestValidCommand:
    def test_host_correlation_and_freshness_are_not_passed_to_the_effector(self):
        parameters = {"target_id": 3, "event_id": 4, "confidence": 0.5,
                      "device_option": {"axis": "left"}}
        command = validate_actuation_command(actuation_payload(
            parameters, command_id="command-7", issued_at=1_700_000_000))
        assert command.command_id == "command-7" and command.issued_at == 1_700_000_000
        assert command.parameters == parameters

    def test_minimal_device_command_has_required_host_identity(self):
        payload = actuation_payload({"target_id": 0})
        command = validate_actuation_command(payload)
        assert command.target_id == 0
        assert command.command_id == payload["command_id"]
        assert command.parameters == {"target_id": 0}

    def test_full_shape_and_device_extensions_are_preserved(self):
        parameters = {"target_id": 3, "event_id": 0, "confidence": 1.0,
                      "device_option": {"axis": "left"}}
        payload = actuation_payload(parameters, command_id="command-0", issued_at=1_700_000_000)
        command = validate_actuation_command(payload)
        assert command.target_id == 3 and command.command_id == "command-0"
        assert command.issued_at == 1_700_000_000.0
        assert command.parameters == parameters
        assert command.parameters is not payload["parameters"]

    def test_nested_device_extensions_are_snapshotted(self):
        payload = actuation_payload({"target_id": 3, "device_option": {"axis": "left"}})
        command = validate_actuation_command(payload)
        payload["parameters"]["device_option"]["axis"] = "right"
        assert command.parameters["device_option"] == {"axis": "left"}

    def test_optional_decoded_fields_may_be_none(self):
        command = validate_actuation_command(actuation_payload({
            "target_id": 1, "event_id": None, "confidence": None,
        }))
        assert command.parameters == {"target_id": 1}


class TestInvalidCommand:
    @pytest.mark.parametrize("parameters, message", [
        ({}, "target_id"), ({"target_id": True}, "target_id"),
        ({"target_id": -1}, "target_id"), ({"target_id": 1.0}, "target_id"),
        ({"target_id": 1, "event_id": True}, "event_id"),
        ({"target_id": 1, "event_id": -1}, "event_id"),
        ({"target_id": 1, "event_id": 2.0}, "event_id"),
        ({"target_id": 1, "confidence": True}, "confidence"),
        ({"target_id": 1, "confidence": "0.5"}, "confidence"),
        ({"target_id": 1, "confidence": float("-inf")}, "confidence"),
        ({"target_id": 1, "confidence": -0.01}, "confidence"),
        ({"target_id": 1, "confidence": 1.01}, "confidence"),
    ])
    def test_rejects_device_values_without_coercion(self, parameters, message):
        with pytest.raises(InvalidActuationCommand, match=message):
            validate_actuation_command(actuation_payload(parameters))

    @pytest.mark.parametrize("payload", [None, [], UserDict({"target_id": 1}), {"target_id": 1}])
    def test_requires_the_v1_envelope(self, payload):
        with pytest.raises(InvalidActuationCommand, match="payload"):
            validate_actuation_command(payload)

    @pytest.mark.parametrize("key,value", [
        ("command_id", None), ("command_id", False), ("command_id", -1), ("command_id", ""),
        ("issued_at", None), ("issued_at", "1.0"), ("issued_at", float("nan")),
        ("issued_at", float("inf")),
    ])
    def test_rejects_host_values_without_coercion(self, key, value):
        payload = actuation_payload()
        payload[key] = value
        with pytest.raises(InvalidActuationCommand, match=key):
            validate_actuation_command(payload)

    @pytest.mark.parametrize("key,value", [
        ("generation", ""), ("trial_number", True), ("trial_number", 1.5), ("trial_number", 0),
        ("processing_revision", ""), ("acquisition_revision", 1), ("forced", 1),
        ("produced_at", "2026-11-21T00:00:00"), ("produced_at", None),
    ])
    def test_rejects_invalid_trial_origin(self, key, value):
        payload = actuation_payload()
        payload["origin"][key] = value
        with pytest.raises(InvalidActuationCommand):
            validate_actuation_command(payload)

    def test_error_carries_only_independently_valid_correlation(self):
        payload = actuation_payload({"target_id": 5, "confidence": float("nan")}, command_id="command-7")
        with pytest.raises(InvalidActuationCommand) as caught:
            validate_actuation_command(payload)
        assert caught.value.target_id == 5
        assert caught.value.command_id == "command-7"
        assert caught.value.origin == payload["origin"]
        payload.update(command_id=False, origin={})
        payload["parameters"]["target_id"] = True
        with pytest.raises(InvalidActuationCommand) as caught:
            validate_actuation_command(payload)
        assert caught.value.target_id is None
        assert caught.value.command_id is None
        assert caught.value.origin is None


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


class TestRouteMirror:
    def test_routes_are_mirror_images(self):
        actuation_side = engine_peer_config("127.0.0.1", 8001)
        engine_side = actuation_peer_config("127.0.0.1", 8001)
        assert actuation_side.routes.outbound == engine_side.routes.inbound
        assert actuation_side.routes.inbound == engine_side.routes.outbound

    def test_actuation_dials_out(self):
        from omnibci.infrastructure.ipc.peer_transport import ConnectTransport
        cfg = engine_peer_config("10.0.0.5", 9001)
        assert isinstance(cfg.transport, ConnectTransport)

    def test_command_inbound_feedback_outbound(self):
        cfg = engine_peer_config("127.0.0.1", 8001)
        assert cfg.routes.inbound == frozenset({EventType.ActuationCommand, EventType.SettleActuation,
                                               EventType.ResumeActuation})
        assert cfg.routes.outbound == frozenset({
            EventType.ActuationAck, EventType.ActuationState, EventType.ActuationFault,
            EventType.CommandResult,
        })
