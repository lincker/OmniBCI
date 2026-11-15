"""Module application replaces only its own peer and preserves the engine workers."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
from omnibci.infrastructure.neuroengine.gateway.routing import PeerConfig, RouteSpec
from omnibci.shared.events import EventType
from tests.neuroengine.test_neuro_engine import _make_engine
from tests.neuroengine.test_peer_gateway import FakeBus, FakeTransport


def config(name, transport):
    return PeerConfig(name, transport, RouteSpec(frozenset({EventType.TrialCompleted}), frozenset()))


@pytest.mark.asyncio
async def test_replacing_stimulation_keeps_actuation_and_worker_routes_alive():
    bus = FakeBus()
    gateway = PeerGateway(bus)
    previous, unrelated, replacement = FakeTransport(), FakeTransport(), FakeTransport()
    gateway.add_peer(config("stimulation", previous))
    gateway.add_peer(config("actuation", unrelated))
    await gateway.start()
    try:
        actuation = gateway.peer("actuation")
        await gateway.replace_peer("stimulation", config("stimulation", replacement))
        assert previous.stop_count == 1 and replacement.start_count == 1
        assert gateway.peer("actuation") is actuation
        assert unrelated.stop_count == 0 and unrelated.start_count == 1
        assert len(bus.listeners[EventType.TrialCompleted]) == 2
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_failed_replacement_can_restore_previous_configuration():
    gateway = PeerGateway(FakeBus())
    old = FakeTransport()
    gateway.add_peer(config("stimulation", old))
    await gateway.start()
    try:
        broken = FakeTransport(fail_start=True)
        with pytest.raises(RuntimeError, match="start failed"):
            await gateway.replace_peer("stimulation", config("stimulation", broken))
        assert broken.stop_count == 1
        restored = FakeTransport()
        await gateway.replace_peer("stimulation", config("stimulation", restored))
        assert restored.start_count == 1
    finally:
        await gateway.stop()


@pytest.mark.asyncio
async def test_failed_candidate_cleanup_stays_owned_until_gateway_stop():
    gateway = PeerGateway(FakeBus())
    gateway.add_peer(config("stimulation", FakeTransport()))
    await gateway.start()
    broken = FakeTransport(fail_start=True, fail_stop=True)
    with pytest.raises(BaseExceptionGroup, match="cleanup failed"):
        await gateway.replace_peer("stimulation", config("stimulation", broken))
    assert gateway.peer("stimulation").has_lifecycle_resources
    with pytest.raises(RuntimeError, match="settled"):
        await gateway.replace_peer("stimulation", config("stimulation", FakeTransport()))
    broken.fail_stop = False
    await gateway.stop()
    assert not gateway.peer("stimulation").has_lifecycle_resources


def snapshot(**changes):
    return {"CONFIG.module_revisions": {"stimulation": "a" * 64, "actuation": "b" * 64},
            "CONFIG.system_revision": "c" * 64, "CONFIG.generation": "run",
            "STIMULATION.auto_spawn": False,
            "ACTUATION.auto_spawn": False, **changes}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["CONFIG.generation", "CONFIG.system_revision"])
async def test_stale_engine_or_system_never_replaces_a_peer(field):
    engine = _make_engine(generation="run", system_revision="c" * 64)
    engine.peer_gateway.replace_peer = AsyncMock()
    engine._loop = asyncio.get_running_loop()
    with pytest.raises(ValueError, match="different engine or system"):
        await engine.reconfigure_output_modules(snapshot(**{field: "stale"}), ["stimulation"])
    engine.peer_gateway.replace_peer.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_replacement_routes_new_identity_and_preserves_processing_workers():
    engine = _make_engine(generation="run", system_revision="c" * 64)
    engine.peer_gateway.replace_peer = AsyncMock()
    engine._loop = asyncio.get_running_loop()
    await engine.reconfigure_output_modules(snapshot(), ["stimulation"])
    role, selected = engine.peer_gateway.replace_peer.call_args.args
    assert role == "stimulation"
    assert selected.transport._server.identity.module_revision == "a" * 64
    assert selected.transport._server.identity.system_revision == "c" * 64
    engine.acquisition_command_queue.put.assert_not_called()
    engine.processor_command_queue.put.assert_not_called()
    engine.acquisition_worker.start.assert_not_called()
    engine.signal_processing_worker.start.assert_not_called()


@pytest.mark.asyncio
async def test_actuation_configuration_keeps_transport_but_requires_explicit_start():
    engine = _make_engine(generation="run", system_revision="c" * 64)
    engine.peer_gateway.replace_peer = AsyncMock()
    engine._loop = asyncio.get_running_loop()
    await engine.reconfigure_output_modules(snapshot(), ["actuation"])
    assert engine.actuation_translator is not None
    assert not engine._actuation_active
    await engine.set_actuation_active(True)
    assert engine._actuation_active
    engine.event_bus.register_event_listener.assert_any_call(
        EventType.TrialCompleted, engine.actuation_translator.on_trial_completed)
    await engine.reconfigure_output_modules(snapshot(), ["actuation"])
    assert not engine._actuation_active
    engine.event_bus.unregister_event_listener.assert_called_with(
        EventType.TrialCompleted, engine.actuation_translator.on_trial_completed)
    assert engine.peer_gateway.replace_peer.call_args.args[1] is not None


@pytest.mark.asyncio
async def test_output_reconfiguration_from_another_loop_executes_on_engine_loop():
    engine_loop = asyncio.get_running_loop()
    engine = _make_engine(generation="run", system_revision="c" * 64)
    engine._loop = engine_loop
    loops = []
    async def replace(role, selected):
        loops.append(asyncio.get_running_loop())
    engine.peer_gateway.replace_peer = AsyncMock(side_effect=replace)
    await asyncio.to_thread(asyncio.run, engine.reconfigure_output_modules(snapshot(), ["stimulation"]))
    assert loops == [engine_loop]
