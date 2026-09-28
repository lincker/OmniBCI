"""Node publication follows runtime ownership, not editable or historical state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnibci.application.config_errors import ConfigProfileConflictError
from omnibci.application.plugin_profiles import ProfileRevisionConflict
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
    running(engine, {"CONFIG.revision": "running", "nested": {"value": 1}})
    selected = Mock(side_effect=AssertionError("must not read saved configuration"))
    state, snapshot = engine.node_configuration_snapshot(selected)
    assert state == "running"
    assert snapshot["CONFIG.revision"] == "running"
    snapshot["nested"]["value"] = 9
    assert engine._applied_snapshot["nested"]["value"] == 1


def test_idle_publication_ignores_historical_applied_snapshot(engine):
    engine._applied_snapshot = {"CONFIG.revision": "old", "CONFIG.generation": "old"}
    assert engine.node_configuration_snapshot(lambda: {"CONFIG.revision": "selected"}) == (
        "selected", {"CONFIG.revision": "selected"})


@pytest.mark.parametrize("phase", ["starting", "stopping", "failed_stop", "dead_thread", "apply", "recovery"])
def test_unsettled_runtime_never_publishes_a_node_configuration(engine, phase):
    selected = Mock(side_effect=AssertionError("must not read saved configuration"))
    if phase in ("starting", "stopping"):
        engine._transitioning = True
    elif phase == "failed_stop":
        engine._restart_blocked = True
    elif phase == "dead_thread":
        engine._running = True
    elif phase == "apply":
        engine.admission._transitioning = True
    else:
        engine.admission.quarantine("uncertain cleanup")
    with pytest.raises((ProfileRevisionConflict, ConfigProfileConflictError)):
        engine.node_configuration_snapshot(selected)


@pytest.mark.parametrize(("role", "snapshot", "message"), [
    ("engine", {}, "role"),
    ("stimulation", {"STIMULATION.auto_spawn": True}, "managed"),
    ("actuation", {"ACTUATION.enabled": False}, "disabled"),
    ("actuation", {"ACTUATION.enabled": True, "ACTUATION.auto_spawn": True}, "managed"),
])
def test_node_endpoint_rejects_unowned_roles(role, snapshot, message):
    engine = SimpleNamespace(node_configuration_snapshot=lambda selected: ("running", snapshot))
    with pytest.raises((ValueError, ConfigProfileConflictError), match=message):
        NodeConfigurationService(engine, Mock()).configuration(role)
