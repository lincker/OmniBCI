"""Execution evidence is immutable, scoped and independent of local paths."""
from copy import deepcopy

import pytest


def receipt_data():
    return {
        "module": "actuation", "operation_id": "prepare-1", "generation": "run-1",
        "module_revision": "a" * 64, "engine_revision": "b" * 64,
        "node_id": "device-node", "target": "linux-aarch64", "environment_sha256": "c" * 64,
        "artifacts": {"actuation.effector": {
            "binding_sha256": "d" * 64, "content_sha256": "e" * 64,
            "origin": "local", "verified_repository": None,
        }},
    }


def test_execution_evidence_cannot_be_changed_through_input_or_export():
    from omnibci.application.plugin_execution import PluginExecutionReceipt

    data = receipt_data()
    expected = deepcopy(data)
    receipt = PluginExecutionReceipt.from_dict(data)
    data["artifacts"].clear()
    receipt.artifacts.clear()
    receipt.to_dict()["artifacts"].clear()
    assert receipt.to_dict() == expected
    assert PluginExecutionReceipt.from_dict(receipt.to_dict()) == receipt


@pytest.mark.parametrize("change", [
    "path", "artifact_path", "unowned", "no_artifacts", "bad_hash", "no_operation",
    "bad_origin", "false_proof", "missing_field",
])
def test_execution_evidence_rejects_invalid_or_excess_information(change):
    from omnibci.application.plugin_execution import PluginExecutionReceipt

    data = receipt_data()
    artifact = data["artifacts"]["actuation.effector"]
    if change == "path":
        data["location"] = "/private/plugins"
    elif change == "artifact_path":
        artifact["location"] = "/private/plugins"
    elif change == "unowned":
        data["artifacts"] = {"stimulation.paradigm": artifact}
    elif change == "no_artifacts":
        data["artifacts"] = {}
    elif change == "bad_hash":
        artifact["content_sha256"] = "not a digest"
    elif change == "no_operation":
        data["operation_id"] = " "
    elif change == "bad_origin":
        artifact["origin"] = "unknown"
    elif change == "false_proof":
        artifact["verified_repository"] = "unverified"
    else:
        del data["environment_sha256"]
    with pytest.raises(ValueError):
        PluginExecutionReceipt.from_dict(data)


def test_peer_admission_checks_operation_and_binding_not_controller_environment():
    from omnibci.application.plugin_execution import PluginExecutionReceipt
    from omnibci.infrastructure.ipc.peer_identity import PeerIdentity

    receipt = PluginExecutionReceipt.from_dict(receipt_data())
    identity = PeerIdentity("actuation", "a" * 64, "b" * 64, "run-1",
                            "prepare-1", receipt.bindings_sha256)
    assert identity.accepts(receipt)
    for field, value in (("operation_id", "prepare-2"), ("generation", "run-2"),
                         ("module_revision", "f" * 64)):
        data = receipt_data()
        data[field] = value
        assert not identity.accepts(PluginExecutionReceipt.from_dict(data))
    data = receipt_data()
    data["artifacts"]["actuation.effector"]["binding_sha256"] = "f" * 64
    assert not identity.accepts(PluginExecutionReceipt.from_dict(data))
