"""Node publication follows runtime ownership, not editable or historical state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnibci.application.configuration.errors import ConfigProfileConflictError
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.services.node_configuration_service import NodeConfigurationService


@pytest.fixture
def engine():
    return NeuroEngineService(engine_factory=Mock())


def running(engine, snapshot):
    engine._engine = Mock()
    engine._running = engine._accepting_commands = True
    engine._engine_thread = SimpleNamespace(is_alive=lambda: True)
    engine._applied_snapshot = snapshot


def test_running_publication_uses_applied_snapshot_without_reading_saved_files(engine):
    running(engine, {"CONFIG.module_revisions": {'acquisition': "running", 'processing': "running", 'stimulation': "running", 'actuation': "running"}, "CONFIG.engine_revision": "c" * 64, "nested": {"value": 1}})
    selected = Mock(side_effect=AssertionError("must not read saved configuration"))
    state, snapshot = engine.node_configuration_snapshot(selected)
    assert state == "running"
    assert snapshot["CONFIG.module_revisions"]["acquisition"] == "running"
    snapshot["nested"]["value"] = 9
    assert engine._applied_snapshot["nested"]["value"] == 1


def test_idle_publication_ignores_historical_applied_snapshot(engine):
    engine._applied_snapshot = {"CONFIG.module_revisions": {'acquisition': "old", 'processing': "old", 'stimulation': "old", 'actuation': "old"}, "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "old"}
    assert engine.node_configuration_snapshot(lambda: {"CONFIG.module_revisions": {'acquisition': "selected", 'processing': "selected", 'stimulation': "selected", 'actuation': "selected"}, "CONFIG.engine_revision": "c" * 64}) == (
        "selected", {"CONFIG.module_revisions": {'acquisition': "selected", 'processing': "selected", 'stimulation': "selected", 'actuation': "selected"}, "CONFIG.engine_revision": "c" * 64})


@pytest.mark.parametrize("phase", ["starting", "stopping", "failed_stop", "dead_thread", "apply", "recovery"])
def test_unsettled_runtime_never_publishes_a_node_configuration(engine, phase):
    selected = Mock(side_effect=AssertionError("must not read saved configuration"))
    reservation = None
    if phase in ("starting", "stopping"):
        engine._transitioning = True
    elif phase == "failed_stop":
        engine._restart_blocked = True
    elif phase == "dead_thread":
        engine._running = True
    elif phase == "apply":
        reservation = engine.admission.reserve_transition()
    else:
        engine.admission.quarantine("uncertain cleanup")
    try:
        with pytest.raises((ProfileRevisionConflict, ConfigProfileConflictError)):
            engine.node_configuration_snapshot(selected)
    finally:
        if reservation is not None:
            engine.admission.release_reservation(reservation)


@pytest.mark.parametrize(("role", "snapshot", "message"), [
    ("engine", {}, "role"),
    ("stimulation", {"STIMULATION.auto_spawn": True}, "managed"),
    ("actuation", {"ACTUATION.auto_spawn": True}, "managed"),
])
def test_node_endpoint_rejects_unowned_roles(role, snapshot, message):
    engine = SimpleNamespace(node_configuration_snapshot=lambda selected, *, module: ("running", snapshot))
    with pytest.raises((ValueError, ConfigProfileConflictError), match=message):
        NodeConfigurationService(engine, Mock()).configuration(role)


def test_unrelated_processing_transition_and_recovery_do_not_block_stimulation_publication(engine):
    reservation = engine.admission.reserve_transition("processing")
    engine.admission.quarantine("Processing acknowledgement missing", "processing")
    try:
        assert engine.node_configuration_snapshot(lambda: {"role": "stimulation"}, module="stimulation") == (
            "selected", {"role": "stimulation"})
        with pytest.raises(ProfileRevisionConflict):
            engine.node_configuration_snapshot(lambda: {}, module="processing")
    finally:
        engine.admission.release_reservation(reservation)


def test_running_node_publication_requires_a_prepared_output_operation(engine):
    running(engine, {"STIMULATION.auto_spawn": False, "CONFIG.generation": "run-1"})
    engine._engine.node_operation_id.return_value = None
    with pytest.raises(ConfigProfileConflictError, match="Prepare"):
        engine.node_configuration_snapshot(Mock(), module="stimulation")


def test_node_operation_is_read_from_the_current_host_without_mutating_applied_state(engine):
    running(engine, {"STIMULATION.auto_spawn": False, "CONFIG.generation": "run-1"})
    engine._engine.node_operation_id.return_value = "operation-1"
    state, snapshot = engine.node_configuration_snapshot(Mock(), module="stimulation")
    assert state == "running"
    assert snapshot["CONFIG.operation_id"] == "operation-1"
    assert "CONFIG.operation_id" not in engine._applied_snapshot
    engine._engine.node_operation_id.return_value = "operation-2"
    assert engine.node_configuration_snapshot(Mock(), module="stimulation")[1]["CONFIG.operation_id"] == "operation-2"
