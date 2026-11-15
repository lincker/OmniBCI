"""Engine listeners pin only externally managed nodes to runtime identities."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.ipc.peer_transport import ListenTransport
from omnibci.infrastructure.neuroengine import builder
from omnibci.infrastructure.neuroengine.gateway.peers import (
    actuation_peer_config, stimulation_peer_config,
)


@pytest_asyncio.fixture
async def build_graph(monkeypatch):
    monkeypatch.setattr(builder.mp, "Queue", MagicMock())
    monkeypatch.setattr(builder.mp, "Pipe", lambda **kwargs: (MagicMock(), MagicMock()))
    monkeypatch.setattr(builder, "AcquisitionWorker", MagicMock())
    monkeypatch.setattr(builder, "SignalProcessingWorker", MagicMock())
    monkeypatch.setattr(ListenTransport, "start", AsyncMock())
    monkeypatch.setattr(ListenTransport, "stop", AsyncMock())
    engines = []

    async def build(**settings):
        snapshot = {"CONFIG.module_revisions": {role: "a" * 64 for role in (
            "acquisition", "processing", "stimulation", "actuation")},
            "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "run-1", **settings}
        engine = builder.NeuroEngineBuilder(settings_snapshot=snapshot).build()
        assert not engine.peer_gateway._channels
        engine._loop = asyncio.get_running_loop()
        engine._started = True
        await engine.peer_gateway.start()
        engines.append(engine)
        return engine, snapshot
    yield build
    for engine in engines:
        await engine.peer_gateway.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stim_managed,actuation_managed", [
    (True, True), (False, True), (True, False), (False, False),
])
async def test_prepared_listener_uses_its_own_external_identity(
    build_graph, stim_managed, actuation_managed,
):
    engine, snapshot = await build_graph(**{
        "STIMULATION.auto_spawn": stim_managed,
        "ACTUATION.auto_spawn": actuation_managed,
    })
    for role, managed in (("stimulation", stim_managed), ("actuation", actuation_managed)):
        await engine.ensure_output_ready(role, snapshot)
        server = engine.peer_gateway.peer(role)._transport._server
        expected = None if managed else PeerIdentity(role, "a" * 64, "c" * 64, "run-1")
        assert server.identity == expected
        assert server.max_clients == (None if role == "stimulation" and managed else 1)


@pytest.mark.asyncio
async def test_preparation_is_idempotent_and_does_not_prepare_another_role(build_graph):
    engine, snapshot = await build_graph()
    await engine.ensure_output_ready("stimulation", snapshot)
    channel = engine.peer_gateway.peer("stimulation")
    await engine.ensure_output_ready("stimulation", snapshot)
    assert engine.peer_gateway.peer("stimulation") is channel
    assert "actuation" not in engine.peer_gateway._channels
    assert channel._transport._server.identity is None


@pytest.mark.asyncio
@pytest.mark.parametrize("settings,address", [
    ({}, ("127.0.0.1", 8765)),
    ({"STIMULATION.server_ip": "192.0.2.4", "STIMULATION.server_port": 9234}, ("192.0.2.4", 9234)),
])
async def test_stimulation_listener_and_client_use_the_same_module_address(build_graph, settings, address):
    from omnibci.infrastructure.stimulation.application import Stimulation

    engine, snapshot = await build_graph(**settings)
    await engine.ensure_output_ready("stimulation", snapshot)
    server = engine.peer_gateway.peer("stimulation")._transport._server
    client = Stimulation(settings_snapshot=snapshot).peer_channel._transport._client
    assert (server.host, server.port) == address
    assert (client.server_ip, client.server_port) == address


@pytest.mark.asyncio
async def test_external_actuation_is_prepared_before_module_activation(build_graph):
    engine, snapshot = await build_graph(**{"ACTUATION.auto_spawn": False})
    assert not engine.peer_gateway._channels
    await engine.ensure_output_ready("actuation", snapshot)
    assert engine.peer_gateway.peer("actuation")._transport._server.identity == PeerIdentity(
        "actuation", "a" * 64, "c" * 64, "run-1")
    assert not engine._actuation_active


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["stimulation", "actuation"])
@pytest.mark.parametrize("missing", ["CONFIG.module_revisions", "CONFIG.engine_revision", "CONFIG.generation"])
async def test_invalid_external_identity_fails_when_preparing_only_that_role(build_graph, role, missing):
    engine, snapshot = await build_graph(**{f"{role.upper()}.auto_spawn": False, missing: None})
    with pytest.raises(ValueError, match="Peer identity"):
        await engine.ensure_output_ready(role, snapshot)
    assert not engine.peer_gateway._channels
    builder.AcquisitionWorker.assert_not_called()
    builder.SignalProcessingWorker.assert_not_called()


def test_peer_factories_preserve_routing_while_limiting_external_stimulation():
    identity = PeerIdentity("stimulation", "a" * 64, "c" * 64, "run-1")
    managed = stimulation_peer_config("127.0.0.1", 8000)
    external = stimulation_peer_config("127.0.0.1", 8000, identity=identity)
    assert managed.routes == external.routes
    assert managed.transport._server.max_clients is None
    assert external.transport._server.max_clients == 1

    identity = PeerIdentity("actuation", "a" * 64, "c" * 64, "run-1")
    managed = actuation_peer_config("127.0.0.1", 8001, watchdog_timeout_s=2)
    external = actuation_peer_config("127.0.0.1", 8001, watchdog_timeout_s=2, identity=identity)
    assert managed.routes == external.routes
    assert managed.transport._server.max_clients == external.transport._server.max_clients == 1
    assert managed.transport._server.idle_timeout == external.transport._server.idle_timeout == 4
