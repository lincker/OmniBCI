"""Transitions exclude whole commands while nested internal commands remain usable."""
import asyncio
from threading import Event, Thread

import pytest

from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.runtime_admission import RuntimeAdmission, admitted


def test_active_operation_blocks_transition_until_all_nested_commands_finish():
    gate = RuntimeAdmission()
    with gate.command():
        with gate.command():
            assert sum(kind == "command" for _, kind in gate._owners.values()) == 1
        with pytest.raises(ProfileRevisionConflict):
            with gate.transition():
                pass
    with gate.transition():
        with gate.command():
            assert sum(kind == "command" for _, kind in gate._owners.values()) == 0


@pytest.mark.asyncio
async def test_transition_owner_passes_to_worker_thread_but_external_calls_reject():
    gate = RuntimeAdmission()
    rejected = Event()

    def external():
        try:
            with gate.command():
                pass
        except ProfileRevisionConflict:
            rejected.set()

    def internal():
        with gate.command():
            return gate.is_transition_owner

    with gate.transition():
        thread = Thread(target=external)
        thread.start()
        thread.join()
        assert rejected.is_set()
        assert await asyncio.to_thread(internal)
    with gate.command():
        pass


@pytest.mark.asyncio
async def test_async_admission_does_not_hold_thread_lock_and_releases_on_failure():
    gate = RuntimeAdmission()
    def probe():
        with gate._lock:
            return True

    class Service:
        _admission = gate

        @admitted
        async def command(self):
            assert await asyncio.to_thread(probe)
            raise ValueError("failed command")
    with pytest.raises(ValueError):
        await Service().command()
    assert not gate._owners


def test_unrelated_modules_can_transition_and_recovery_is_scoped():
    gate = RuntimeAdmission()
    reservation = gate.reserve_transition("processing")
    with gate.command("stimulation"):
        pass
    with gate.transition("actuation"):
        pass
    with pytest.raises(ProfileRevisionConflict):
        with gate.command("processing"):
            pass
    gate.release_reservation(reservation)
    gate.quarantine("Unknown output result", "actuation")
    with gate.command("stimulation"):
        pass
    with pytest.raises(ProfileRevisionConflict):
        with gate.command("actuation"):
            pass
    with gate.command("actuation", recovery=True):
        with gate.command("actuation"):
            pass


@pytest.mark.asyncio
async def test_stop_waits_for_owner_and_blocks_new_work_without_blocking_other_modules():
    gate = RuntimeAdmission()
    reservation = gate.reserve_transition("processing")
    stopped = asyncio.Event()

    async def stop():
        async with gate.stop_transition("processing"):
            stopped.set()

    task = asyncio.create_task(stop())
    await asyncio.sleep(0)
    assert not stopped.is_set()
    with pytest.raises(ProfileRevisionConflict):
        gate.reserve_transition("processing")
    with gate.transition("stimulation"):
        pass
    with gate.transition("processing", reservation=reservation):
        assert await asyncio.to_thread(lambda: nested_command(gate))
    await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    with gate.transition("processing"):
        pass


def nested_command(gate):
    with gate.command("processing"):
        return gate.is_transition_owner


def test_nested_command_cannot_expand_its_owners_scope():
    gate = RuntimeAdmission()
    with gate.transition("stimulation"):
        with pytest.raises(ProfileRevisionConflict):
            with gate.command("actuation"):
                pass


@pytest.mark.parametrize("role", ["stimulation", "actuation"])
def test_engine_scope_excludes_outputs_but_server_shutdown_includes_them(role):
    gate = RuntimeAdmission()
    reservation = gate.reserve_transition(role)
    with gate.transition("engine"):
        assert not gate.owns("actuation") and not gate.owns("stimulation")
    with pytest.raises(ProfileRevisionConflict):
        gate.reserve_transition("all")
    gate.release_reservation(reservation)
    with gate.transition("engine"):
        assert gate.owns("processing")


@pytest.mark.asyncio
async def test_synchronous_stop_waits_for_async_owner_and_preserves_internal_stops():
    gate = RuntimeAdmission()
    reservation = gate.reserve_transition("stimulation")
    waiting, stopped = Event(), Event()

    def stop():
        waiting.set()
        with gate.stop_transition_sync("stimulation"):
            stopped.set()

    thread = Thread(target=stop)
    thread.start()
    assert waiting.wait(1)
    with gate.transition("stimulation", reservation=reservation):
        with gate.stop_transition_sync("stimulation"):
            assert not stopped.is_set()
    await asyncio.to_thread(thread.join, 1)
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_engine_stop_waits_for_existing_module_stop():
    gate = RuntimeAdmission()
    entered, release = asyncio.Event(), asyncio.Event()
    order = []

    async def stop_module():
        async with gate.stop_transition("processing"):
            entered.set()
            await release.wait()
            order.append("module")

    async def stop_engine():
        async with gate.stop_transition("engine"):
            order.append("engine")

    module = asyncio.create_task(stop_module())
    await entered.wait()
    engine = asyncio.create_task(stop_engine())
    await asyncio.sleep(0)
    assert not engine.done()
    release.set()
    await asyncio.gather(module, engine)
    assert order == ["module", "engine"]


def test_clearing_foundation_recovery_preserves_unconfirmed_module_state():
    gate = RuntimeAdmission()
    gate.quarantine("External output not settled", "actuation")
    gate.quarantine("Foundation not stopped", "engine")
    gate.clear_quarantine("engine")
    assert gate.recovery_status == {"actuation": "External output not settled"}
    gate.clear_quarantine()
    assert gate.recovery_status == {}


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
