"""Unit tests for ActuationAppService."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.actuation_service import ActuationAppService
from omnibci.shared.events import EventType


def _snapshot(*, auto_spawn=True, revision="act-rev-1"):
    return {
        "CONFIG.generation": "gen-1",
        "CONFIG.engine_revision": "eng-rev-1",
        "CONFIG.module_revisions": {"actuation": revision},
        "ACTUATION.auto_spawn": auto_spawn,
    }


@pytest.fixture
def harness():
    engine = SimpleNamespace(
        is_engine_ready=True,
        applied_snapshot=_snapshot(),
        admission=RuntimeAdmission(),
        is_peer_connected=MagicMock(return_value=False),
        ensure_output_ready=AsyncMock(),
        set_actuation_active=AsyncMock(),
    )
    process_manager = MagicMock(is_alive=False, current_effector="acme.logger/default@1.0.0")
    process_manager.start.side_effect = lambda **_: setattr(process_manager, "is_alive", True)
    process_manager.stop.side_effect = lambda: setattr(process_manager, "is_alive", False)
    commands = MagicMock()
    service = ActuationAppService(engine, process_manager, commands)
    return service, engine, process_manager, commands


@pytest.mark.asyncio
async def test_managed_start_and_stop_lifecycle(harness):
    service, engine, process_manager, _ = harness
    snap = _snapshot(auto_spawn=True)

    assert not service.is_active
    assert not service.process_alive
    assert service.current_effector == "acme.logger/default@1.0.0"
    assert service.owned_snapshot is None

    await service.start(snap)

    engine.ensure_output_ready.assert_awaited_once_with("actuation", snap)
    process_manager.start.assert_called_once_with(settings_snapshot=snap)
    engine.set_actuation_active.assert_awaited_once_with(True)
    assert service.process_alive
    assert service.is_active
    assert service.owned_snapshot == snap

    await service.stop()

    engine.set_actuation_active.assert_awaited_with(False)
    process_manager.stop.assert_called_once_with()
    assert not service.process_alive
    assert not service.is_active
    assert service.owned_snapshot is None


@pytest.mark.asyncio
async def test_external_start_and_stop_require_peer_and_confirmed_receipts(harness):
    service, engine, process_manager, commands = harness
    snap = _snapshot(auto_spawn=False)

    with pytest.raises(RuntimeError, match="Connect the external Actuation node"):
        await service.start(snap)
    assert not service.is_active

    engine.is_peer_connected.return_value = True
    commands.request.return_value = SimpleNamespace(data={
        "generation": "gen-1",
        "engine_revision": "eng-rev-1",
        "module_revision": "act-rev-1",
        "resumed": True,
    })

    await service.start(snap)
    commands.request.assert_called_once_with(EventType.ResumeActuation)
    process_manager.start.assert_not_called()
    assert service.is_active

    commands.request.reset_mock()
    commands.request.return_value = SimpleNamespace(data={
        "generation": "gen-1",
        "engine_revision": "eng-rev-1",
        "module_revision": "act-rev-1",
        "paused": True,
        "execution_complete": True,
        "outputs_settled": True,
    })
    await service.stop()
    commands.request.assert_called_once_with(EventType.SettleActuation)
    assert not service.is_active
    assert service.owned_snapshot is None


@pytest.mark.asyncio
async def test_invalid_receipt_rejects_external_start_and_cleans_up(harness):
    service, engine, _, commands = harness
    snap = _snapshot(auto_spawn=False)
    engine.is_peer_connected.return_value = True
    commands.request.side_effect = [
        SimpleNamespace(data={"generation": "wrong-gen", "resumed": True}),
        SimpleNamespace(data={
            "generation": "gen-1", "engine_revision": "eng-rev-1", "module_revision": "act-rev-1",
            "paused": True, "execution_complete": True, "outputs_settled": True,
        }),
    ]

    with pytest.raises(RuntimeError, match="did not confirm"):
        await service.start(snap)

    assert not service.is_active
    assert not engine.admission.requires_recovery("actuation")


def test_reconnect_updates_only_when_ownership_is_active(harness):
    service, _, _, _ = harness
    updated = _snapshot(revision="act-rev-2")

    service.reconnect(updated)
    assert service.owned_snapshot is None

    service._snapshot = _snapshot(revision="act-rev-1")
    service.reconnect(updated)
    assert service.owned_snapshot == updated
