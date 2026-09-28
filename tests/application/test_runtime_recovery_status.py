"""Current quarantine is independent of retained failed-operation history."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnibci.application.commands.engine_commands import StopEngineCommand
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.runtime_admission import RuntimeAdmission
from omnibci.application.services.engine_service import NeuroEngineService
from omnibci.presentation.schemas.runtime_config import RuntimeConfigurationResponse
from tests.application.test_runtime_configuration_service import runtime


def failed_history(service, revision):
    identifier = str(uuid4())
    operation = dict(operation_id=identifier, plan_id=str(uuid4()), target_revision=revision,
                     status="recovery_required", stage="starting", error="Startup outcome unknown",
                     rolled_back=False, result=None)
    service._operations[identifier] = operation
    service._active_operation = identifier
    return operation


def test_recovery_flag_reflects_quarantine_even_when_engine_is_not_ready(runtime):
    service, bindings, snapshot = runtime
    service.engine.is_engine_ready = False
    service.engine.admission = gate = RuntimeAdmission()
    failed = failed_history(service, snapshot.revision)
    gate.quarantine("Unknown startup outcome")
    status = RuntimeConfigurationResponse.model_validate(service.status())
    assert status.running is False
    assert status.recovery_required is True
    assert status.operation.model_dump() == failed
    plan = service.plan(bindings, expected_revision=snapshot.revision)
    with pytest.raises(ProfileRevisionConflict, match="uncertain runtime"):
        service.submit(plan.plan_id, str(uuid4()))


@pytest.mark.asyncio
async def test_confirmed_stop_clears_current_recovery_and_allows_apply_with_failed_history(runtime):
    service, bindings, snapshot = runtime
    gate = RuntimeAdmission()
    service.engine = NeuroEngineService(lambda: None, admission=gate)
    failed = failed_history(service, snapshot.revision)
    gate.quarantine("Unknown startup outcome")
    assert service.status()["recovery_required"] is True
    await service.engine.handle_stop_engine(StopEngineCommand())
    status = RuntimeConfigurationResponse.model_validate(service.status())
    assert status.running is False
    assert status.recovery_required is False
    assert status.operation.model_dump() == failed
    with gate.command():
        pass
    service.executor = SimpleNamespace(supported_strategies={"select_on_start"},
                                       execute=AsyncMock(return_value={"outcome": "selected"}))
    plan = service.plan(bindings, expected_revision=snapshot.revision)
    operation = service.submit(plan.plan_id, str(uuid4()))
    await service.wait_for_idle()
    assert service.operation(operation["operation_id"])["status"] == "succeeded"
    assert service.operation(failed["operation_id"]) == failed


def test_public_recovery_flag_changes_only_when_quarantine_is_explicitly_cleared():
    gate = RuntimeAdmission()
    assert gate.recovery_required is False
    gate.quarantine("Unconfirmed device state")
    assert gate.recovery_required is True
    with gate.command(recovery=True):
        pass
    assert gate.recovery_required is True
    gate.clear_quarantine()
    assert gate.recovery_required is False
