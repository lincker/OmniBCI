"""Independent module operations preserve unrelated configuration and resources."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnibci.application.configuration.models import EngineSettings
from omnibci.application.configuration.service import ConfigurationService
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.application.configuration.accepted import accepted_runtime_settings


@pytest.mark.asyncio
async def test_module_acknowledgement_and_rollback_preserve_concurrent_output_update():
    service = NeuroEngineService(Mock())
    service._engine = Mock()
    service._running = service._accepting_commands = True
    service._engine_thread = Mock(is_alive=Mock(return_value=True))
    original = {
        "CONFIG.generation": "generation", "CONFIG.engine_revision": "engine",
        "CONFIG.module_revisions": {"stimulation": "s0", "actuation": "e0"},
        "STIMULATION.renderer": "old", "ACTUATION.server_port": 1234,
        "PLUGINS.bindings": [
            {"slot": "stimulation.paradigm", "parameters": {"value": 0}},
            {"slot": "actuation.effector", "parameters": {"value": 0}},
        ],
    }
    service._applied_snapshot = deepcopy(original)
    first, second = deepcopy(original), deepcopy(original)
    first["CONFIG.module_revisions"]["stimulation"] = "s1"
    first["STIMULATION.renderer"] = "new"
    first["PLUGINS.bindings"][0]["parameters"]["value"] = 1
    second["CONFIG.module_revisions"]["actuation"] = "e1"
    second["ACTUATION.server_port"] = 5678
    second["PLUGINS.bindings"][1]["parameters"]["value"] = 2
    async with service.runtime_transition("stimulation"):
        service.acknowledge_configuration(first, modules=("stimulation",))
    async with service.runtime_transition("actuation"):
        service.acknowledge_configuration(second, modules=("actuation",))
    applied = service.applied_snapshot
    assert applied["CONFIG.module_revisions"] == {"stimulation": "s1", "actuation": "e1"}
    assert applied["STIMULATION.renderer"] == "new"
    assert applied["ACTUATION.server_port"] == 5678
    async with service.runtime_transition("actuation"):
        service.acknowledge_configuration(original, modules=("actuation",))
    assert service.applied_snapshot["STIMULATION.renderer"] == "new"
    assert service.applied_revisions["stimulation"] == "s1"


def test_foundation_settings_do_not_parse_idle_module_files():
    repository = SimpleNamespace(
        get_engine=Mock(return_value=EngineSettings({})),
        get_module=Mock(side_effect=ValueError("idle module is corrupt")),
    )
    profiles = ConfigurationService(repository)
    profiles.materialize = lambda selected: selected
    selected = accepted_runtime_settings(profiles=profiles, active_kinds=set())
    assert selected.engine is not None
    repository.get_module.assert_not_called()
