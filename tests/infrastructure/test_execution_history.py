"""Execution evidence is retained independently of editable configuration history."""
import json
from unittest.mock import Mock

import pytest

from omnibci.application.plugin_execution import PluginExecutionReceipt
from omnibci.infrastructure.plugins.preparation import execution_receipt
from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError, StartupReport, StartupReporter, wait_for_startup_report,
)
from tests.execution_helpers import synthetic_execution_snapshot
from tests.infrastructure.test_child_runtime_snapshot import MANAGERS, _mock_spawning, _snapshot


def evidence():
    snapshot = synthetic_execution_snapshot({"CONFIG.generation": "run-1",
        "CONFIG.module_revisions": {"actuation": "a" * 64}, "CONFIG.engine_revision": "b" * 64})
    return execution_receipt(snapshot, "actuation", "prepare-1")


@pytest.mark.parametrize("change", [None, "operation_id", "environment_sha256", "content_sha256", "missing"])
def test_managed_readiness_requires_exact_local_execution_evidence(change):
    expected = evidence()
    data = expected.to_dict()
    if change == "content_sha256":
        data["artifacts"]["actuation.effector"][change] = "0" * 64
    elif change not in (None, "missing"):
        data[change] = "0" * 64
    actual = None if change == "missing" else PluginExecutionReceipt.from_dict(data)
    receiver = Mock(poll=Mock(return_value=True), recv=Mock(return_value=StartupReport.ready("Actuation", execution=actual)))
    process = Mock(is_alive=Mock(return_value=True))
    if change is None:
        assert wait_for_startup_report(receiver, process, component="Actuation", timeout_s=1,
                                       expected_execution=expected).execution == expected
    else:
        with pytest.raises(ChildProcessStartupError, match="execution"):
            wait_for_startup_report(receiver, process, component="Actuation", timeout_s=1,
                                    expected_execution=expected)


def test_processing_standby_does_not_report_plugin_execution():
    connection = Mock()
    reporter = StartupReporter(connection, component="Signal Processing")
    reporter.ready()
    assert connection.send.call_args.args[0].execution is None


def test_history_is_idempotent_and_does_not_relabel_an_operation(tmp_path):
    from omnibci.infrastructure.plugins.execution_history import PluginExecutionHistory

    history = PluginExecutionHistory(tmp_path)
    receipt = evidence()
    history.record(receipt)
    files = list(tmp_path.rglob("*.json"))
    assert len(files) == 1
    content = files[0].read_bytes()
    history.record(PluginExecutionReceipt.from_dict(receipt.to_dict()))
    assert files[0].read_bytes() == content
    assert json.loads(content)["receipt"] == receipt.to_dict()
    data = receipt.to_dict()
    data["artifacts"]["actuation.effector"]["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="different"):
        history.record(PluginExecutionReceipt.from_dict(data))
    assert files[0].read_bytes() == content


def test_history_write_failure_is_explicit_and_leaves_no_success_record(tmp_path, monkeypatch):
    from omnibci.infrastructure.plugins import execution_history

    monkeypatch.setattr(execution_history, "atomic_write", Mock(side_effect=OSError("storage unavailable")))
    with pytest.raises(OSError, match="storage unavailable"):
        execution_history.PluginExecutionHistory(tmp_path).record(evidence())
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("module,manager_class,component,key", MANAGERS)
@pytest.mark.parametrize("history_failure", [False, True])
def test_output_manager_records_only_matching_ready_child_and_cleans_up_history_failure(
    monkeypatch, module, manager_class, component, key, history_failure,
):
    factory = _mock_spawning(monkeypatch, module, component)
    record = Mock(side_effect=OSError("history unavailable") if history_failure else None)
    manager = manager_class(record_execution=record)
    try:
        if history_failure:
            with pytest.raises(OSError, match="history unavailable"):
                manager.start(settings_snapshot=_snapshot(key))
            assert not manager.is_ready
            assert manager._process is None
        else:
            manager.start(settings_snapshot=_snapshot(key))
            assert manager.is_ready
        source = factory.call_args.kwargs["kwargs"]["settings_snapshot"]
        record.assert_called_once_with(execution_receipt(source, component.lower(), source["CONFIG.operation_id"]))
    finally:
        manager.stop()


@pytest.mark.parametrize("module,manager_class,component,key", MANAGERS)
def test_new_child_with_same_configuration_has_a_distinct_execution_operation(
    monkeypatch, module, manager_class, component, key,
):
    _mock_spawning(monkeypatch, module, component)
    record = Mock()
    manager = manager_class(record_execution=record)
    try:
        manager.start(settings_snapshot=_snapshot(key))
        manager.stop()
        manager.start(settings_snapshot=_snapshot(key))
        receipts = [call.args[0] for call in record.call_args_list]
        assert len(receipts) == 2
        assert receipts[0].operation_id != receipts[1].operation_id
        assert receipts[0].module_revision == receipts[1].module_revision
        assert receipts[0].artifacts == receipts[1].artifacts
    finally:
        manager.stop()
