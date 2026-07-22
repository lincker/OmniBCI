"""Actuation-specific validation at the IPC -> device trust boundary."""

from collections import UserDict

import pytest

from omnibci.infrastructure.actuation.contracts import (
    InvalidActuationCommand,
    validate_actuation_command,
)


class TestValidCommand:
    def test_minimal_older_sender_shape(self):
        command = validate_actuation_command({"target_id": 0})
        assert command.target_id == 0
        assert command.seq is None and command.issued_at is None
        assert command.payload == {"target_id": 0}

    def test_full_shape_and_device_extensions_are_preserved(self):
        payload = {
            "target_id": 3,
            "seq": 0,
            "issued_at": 1_700_000_000,
            "event_id": 0,
            "confidence": 1.0,
            "device_option": {"axis": "left"},
        }
        command = validate_actuation_command(payload)
        assert command.target_id == 3 and command.seq == 0
        assert command.issued_at == 1_700_000_000.0
        assert command.payload == payload
        assert command.payload is not payload

    def test_nested_device_extensions_are_snapshotted(self):
        payload = {"target_id": 3, "device_option": {"axis": "left"}}

        command = validate_actuation_command(payload)
        payload["device_option"]["axis"] = "right"

        assert command.payload["device_option"] == {"axis": "left"}

    def test_optional_decoded_fields_may_be_none(self):
        command = validate_actuation_command({
            "target_id": 1, "event_id": None, "confidence": None,
        })
        assert command.payload["event_id"] is None
        assert command.payload["confidence"] is None


class TestInvalidCommand:
    @pytest.mark.parametrize("payload, message", [
        (None, "payload"),
        ([], "payload"),
        (UserDict({"target_id": 1}), "payload"),
        ({}, "target_id"),
        ({"target_id": True}, "target_id"),
        ({"target_id": -1}, "target_id"),
        ({"target_id": 1.0}, "target_id"),
        ({"target_id": 1, "seq": None}, "seq"),
        ({"target_id": 1, "seq": False}, "seq"),
        ({"target_id": 1, "seq": -1}, "seq"),
        ({"target_id": 1, "issued_at": None}, "issued_at"),
        ({"target_id": 1, "issued_at": "1.0"}, "issued_at"),
        ({"target_id": 1, "issued_at": float("nan")}, "issued_at"),
        ({"target_id": 1, "issued_at": float("inf")}, "issued_at"),
        ({"target_id": 1, "event_id": True}, "event_id"),
        ({"target_id": 1, "event_id": -1}, "event_id"),
        ({"target_id": 1, "event_id": 2.0}, "event_id"),
        ({"target_id": 1, "confidence": True}, "confidence"),
        ({"target_id": 1, "confidence": "0.5"}, "confidence"),
        ({"target_id": 1, "confidence": float("-inf")}, "confidence"),
        ({"target_id": 1, "confidence": -0.01}, "confidence"),
        ({"target_id": 1, "confidence": 1.01}, "confidence"),
    ])
    def test_rejects_without_coercion(self, payload, message):
        with pytest.raises(InvalidActuationCommand, match=message):
            validate_actuation_command(payload)

    def test_error_carries_only_independently_valid_correlation(self):
        with pytest.raises(InvalidActuationCommand) as caught:
            validate_actuation_command({
                "target_id": 5,
                "seq": 7,
                "confidence": float("nan"),
            })
        assert caught.value.target_id == 5
        assert caught.value.seq == 7

        with pytest.raises(InvalidActuationCommand) as caught:
            validate_actuation_command({"target_id": True, "seq": False})
        assert caught.value.target_id is None
        assert caught.value.seq is None
