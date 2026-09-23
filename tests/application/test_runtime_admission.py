"""Transitions exclude whole commands while nested internal commands remain usable."""
import asyncio
from threading import Event, Thread

import pytest

from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.runtime_admission import RuntimeAdmission, admitted


def test_active_operation_blocks_transition_until_all_nested_commands_finish():
    gate = RuntimeAdmission()
    with gate.command():
        with gate.command():
            assert gate._active == 1
        with pytest.raises(ProfileRevisionConflict):
            with gate.transition():
                pass
    with gate.transition():
        with gate.command():
            assert gate._active == 0


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
    assert gate._active == 0
