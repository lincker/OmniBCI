"""Current configuration and its user action have one backend owner."""
import asyncio
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from omnibci.application.plugin_profiles import ModuleActivation, ProfileBinding, PluginSelection, ProfileRevisionConflict, SystemSettings
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.runtime_configuration_service import RuntimeConfigurationService
from omnibci.application.services.engine_service import NeuroEngineService


@pytest.fixture
def current_action():
    module = ModuleActivation("acquisition", {}, (ProfileBinding(
        slot="acquisition.source", kind="acquisition", profile_id=None,
        selection=PluginSelection("test.source", "default", "1.0.0"),
        parameters={}, configuration_version=1,
    ),))
    system = SystemSettings({})
    repository = Mock()
    repository.get_module.side_effect = lambda section: module if section == "acquisition" else None
    repository.get_system.return_value = system
    profiles = Mock(repository=repository)
    profiles.propose.return_value = module
    engine = SimpleNamespace(
        admission=RuntimeAdmission(), is_engine_ready=True, generation="engine",
        applied_revisions={"acquisition": module.revision, "system": system.revision},
        applied_snapshot={}, restart_blocked=False, runtime_package_snapshots=(),
        is_module_running=lambda _: False, is_peer_connected=lambda _: False,
    )
    executor = SimpleNamespace(
        active_kinds=lambda _: {"acquisition"},
        acquisition=SimpleNamespace(handle_get_status=lambda _: SimpleNamespace(connected=False, streaming=False)),
        execute=AsyncMock(return_value={"outcome": "no_change"}),
        execute_current_action=AsyncMock(return_value="connected"),
        prepare_processing=AsyncMock(),
    )
    service = RuntimeConfigurationService(profiles, engine, executor=executor)
    configuration = {"expected_revision": module.revision, "settings": {}, "bindings": []}
    return service, executor, configuration


@pytest.mark.asyncio
async def test_connect_keeps_ownership_after_apply_until_connection_is_confirmed(current_action):
    service, executor, configuration = current_action
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(*args):
        assert service.engine.admission.owns("acquisition")
        entered.set()
        await release.wait()
        return "connected"

    executor.execute_current_action.side_effect = connect
    operation_id = str(uuid4())
    task = asyncio.create_task(service.execute_current("acquisition", "connect", operation_id=operation_id, configuration=configuration))
    await asyncio.wait_for(entered.wait(), 1)
    with pytest.raises(ProfileRevisionConflict):
        service.engine.admission.reserve_transition("acquisition")
    assert service.operation(operation_id)["result"]["configuration_applied"] is True
    assert service.operation(operation_id)["status"] == "applying"
    release.set()
    result = await task
    assert result["status"] == "succeeded"
    assert result["result"]["action"] == "connected"


@pytest.mark.asyncio
async def test_current_action_is_idempotent_and_survives_http_cancellation(current_action):
    service, executor, configuration = current_action
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(*args):
        entered.set()
        await release.wait()
        return "connected"

    executor.execute_current_action.side_effect = connect
    operation_id = str(uuid4())
    task = asyncio.create_task(service.execute_current("acquisition", "connect", operation_id=operation_id, configuration=configuration))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProfileRevisionConflict):
        await service.execute_current("acquisition", "connect", operation_id=operation_id,
                                      configuration={**configuration, "settings": {"changed": True}})
    retry = asyncio.create_task(service.execute_current("acquisition", "connect", operation_id=operation_id, configuration=deepcopy(configuration)))
    release.set()
    assert (await retry)["status"] == "succeeded"
    executor.execute.assert_awaited_once()
    executor.execute_current_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_waits_for_cancelled_connection_request_to_finish(current_action):
    service, executor, configuration = current_action
    entered, release, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
    order = []

    async def connect(*args):
        entered.set()
        await release.wait()
        order.append("connected")
        return "connected"

    async def stop():
        stopping.set()
        async with service.engine.admission.stop_transition("acquisition"):
            order.append("stopped")

    executor.execute_current_action.side_effect = connect
    request = asyncio.create_task(service.execute_current(
        "acquisition", "connect", operation_id=str(uuid4()), configuration=configuration))
    await asyncio.wait_for(entered.wait(), 1)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    stop_task = asyncio.create_task(stop())
    try:
        await asyncio.wait_for(stopping.wait(), 1)
        assert not stop_task.done()
        assert order == []
    finally:
        release.set()
        await asyncio.wait_for(stop_task, 1)
        await service.wait_for_idle()
    assert order == ["connected", "stopped"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["acquisition", "system"])
async def test_current_action_rejects_its_own_or_foundation_recovery(current_action, scope):
    service, executor, configuration = current_action
    service.engine.admission.quarantine("Unconfirmed ownership", scope)
    with pytest.raises(ProfileRevisionConflict, match="uncertain runtime module"):
        await service.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=configuration)
    executor.execute.assert_not_awaited()
    executor.execute_current_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_connection_retains_applied_configuration_result(current_action):
    service, executor, configuration = current_action
    executor.execute_current_action.side_effect = RuntimeError("Source refused connection")
    result = await service.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=configuration)
    assert result["status"] == "failed"
    assert result["result"]["configuration_applied"] is True
    assert result["error"] == "Source refused connection"


@pytest.mark.asyncio
async def test_processing_recovery_does_not_block_acquisition_connection(current_action):
    service, executor, configuration = current_action
    service.engine.admission.quarantine("old input failed", "processing")
    result = await service.execute_current("acquisition", "connect", operation_id=str(uuid4()), configuration=configuration)
    assert result["status"] == "succeeded"
    assert service.engine.admission.recovery_status == {"processing": "old input failed"}


@pytest.fixture
def reading_engine():
    engine = NeuroEngineService(Mock())
    engine._running = engine._accepting_commands = True
    engine._engine = Mock()
    engine._engine_thread = Mock(is_alive=lambda: True)
    engine._applied_snapshot = {"CONFIG.generation": "generation", "CONFIG.module_revisions": {"acquisition": "revision"}}
    engine._acquisition = Mock(handle_get_status=lambda _: SimpleNamespace(connected=True, connection_id="connection"))
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["generation", "expected_revision", "connection_id"])
async def test_reading_rejects_stale_identity_without_dispatching(reading_engine, field):
    identity = {"generation": "generation", "expected_revision": "revision", "connection_id": "connection"}
    identity[field] = "stale"
    with pytest.raises(ProfileRevisionConflict, match="Source connection changed"):
        await reading_engine.start_reading(**identity)
    reading_engine._acquisition.handle_start_stream.assert_not_called()


@pytest.mark.asyncio
async def test_reading_checks_identity_in_owned_task_and_does_not_merge_other_requests(reading_engine):
    entered, release = threading.Event(), threading.Event()
    def read(*args, **kwargs):
        assert reading_engine.admission.owns("acquisition")
        entered.set()
        assert release.wait(3)
    reading_engine._acquisition.handle_start_stream.side_effect = read
    request = asyncio.create_task(reading_engine.start_reading(
        generation="generation", expected_revision="revision", connection_id="connection"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(ProfileRevisionConflict, match="in progress"):
            await reading_engine.start_reading(generation="stale", expected_revision="revision", connection_id="connection")
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(ProfileRevisionConflict):
            reading_engine.admission.reserve_transition("acquisition")
    finally:
        release.set()
        await reading_engine._lifecycle_tasks[("read", "acquisition")]
    reading_engine._acquisition.handle_start_stream.assert_called_once()
    reading_engine._acquisition.prepare_processing.assert_called_once()
