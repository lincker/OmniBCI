"""Actuation-specific validation at the IPC -> device trust boundary."""
from collections import UserDict

import pytest

from omnibci.application.commands.actuation_commands import InvalidActuationCommand, validate_actuation_command
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
