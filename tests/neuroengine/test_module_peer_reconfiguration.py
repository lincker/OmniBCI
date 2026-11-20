"""Module application replaces only its own peer and preserves the engine workers."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from omnibci.infrastructure.neuroengine.gateway.peer_gateway import PeerGateway
from omnibci.infrastructure.ipc.routing import PeerConfig, RouteSpec
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
    with pytest.raises(BaseExceptionGroup, match="cleanup failed"):
        await gateway.replace_peer("stimulation", config("stimulation", FakeTransport()))
    broken.fail_stop = False
    await gateway.stop()
    assert not gateway.peer("stimulation").has_lifecycle_resources


def snapshot(**changes):
    return {"CONFIG.module_revisions": {"stimulation": "a" * 64, "actuation": "b" * 64},
            "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "run",
            "STIMULATION.auto_spawn": False,
            "ACTUATION.auto_spawn": False, **changes}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["CONFIG.generation", "CONFIG.engine_revision"])
async def test_stale_engine_generation_or_configuration_never_replaces_a_peer(field):
    engine = _make_engine(generation="run", engine_revision="c" * 64)
    engine.peer_gateway.replace_peer = AsyncMock()
    engine._loop = asyncio.get_running_loop()
    with pytest.raises(ValueError, match="different Engine generation or configuration"):
        await engine.reconfigure_output_modules(snapshot(**{field: "stale"}), ["stimulation"])
    engine.peer_gateway.replace_peer.assert_not_awaited()


@pytest.mark.asyncio
async def test_output_replacement_routes_new_identity_and_preserves_processing_workers():
    engine = _make_engine(generation="run", engine_revision="c" * 64)
    engine.peer_gateway.replace_peer = AsyncMock()
    engine._loop = asyncio.get_running_loop()
    await engine.reconfigure_output_modules(snapshot(), ["stimulation"])
    role, selected = engine.peer_gateway.replace_peer.call_args.args
    assert role == "stimulation"
    assert selected.transport._server.identity.module_revision == "a" * 64
    assert selected.transport._server.identity.engine_revision == "c" * 64
    engine.acquisition_command_queue.put.assert_not_called()
    engine.processor_command_queue.put.assert_not_called()
    engine.acquisition_worker.start.assert_not_called()
    engine.signal_processing_worker.start.assert_not_called()


@pytest.mark.asyncio
async def test_actuation_configuration_keeps_transport_but_requires_explicit_start():
    engine = _make_engine(generation="run", engine_revision="c" * 64)
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
    engine = _make_engine(generation="run", engine_revision="c" * 64)
    engine._loop = engine_loop
    loops = []
    async def replace(role, selected):
        loops.append(asyncio.get_running_loop())
    engine.peer_gateway.replace_peer = AsyncMock(side_effect=replace)
    await asyncio.to_thread(asyncio.run, engine.reconfigure_output_modules(snapshot(), ["stimulation"]))
    assert loops == [engine_loop]


@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["stimulation", "actuation"])
async def test_live_listener_address_change_preserves_other_modules(module, unused_tcp_port_factory):
    ports = {role: unused_tcp_port_factory() for role in ("stimulation", "actuation")}
    bus = FakeBus()
    gateway = PeerGateway(bus)
    engine = _make_engine(event_bus=bus, peer_gateway=gateway, generation="run", engine_revision="c" * 64)
    engine._loop = asyncio.get_running_loop()
    engine._started = True
    original = snapshot(**{f"{role.upper()}.server_port": port for role, port in ports.items()})
    workers = engine.acquisition_worker, engine.signal_processing_worker
    await gateway.start()
    try:
        for role in ports:
            await engine.ensure_output_ready(role, original)
        other = "actuation" if module == "stimulation" else "stimulation"
        unchanged = gateway.peer(other)
        old = gateway.peer(module)
        new_port = unused_tcp_port_factory()
        changed = {**original, f"{module.upper()}.server_port": new_port,
                   "CONFIG.module_revisions": {**original["CONFIG.module_revisions"], module: "d" * 64}}
        await engine.ensure_output_ready(module, changed)
        replacement = gateway.peer(module)
        assert replacement is not old and replacement.is_ready
        assert gateway.peer(other) is unchanged and unchanged.is_ready
        assert replacement._transport._server.port == new_port
        assert engine._generation == "run" and engine._engine_revision == "c" * 64
        assert (engine.acquisition_worker, engine.signal_processing_worker) == workers
        reader, writer = await asyncio.open_connection("127.0.0.1", new_port)
        writer.close()
        await writer.wait_closed()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", ports[module])
        for worker in workers:
            worker.start.assert_not_called()
            worker.join.assert_not_called()
    finally:
        await gateway.stop()
