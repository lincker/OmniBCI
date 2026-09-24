"""Engine listeners pin only externally managed nodes to runtime identities."""

from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.neuroengine import builder
from omnibci.infrastructure.neuroengine.gateway.peers import (
    actuation_peer_config, stimulation_peer_config,
)


@pytest.fixture
def build_graph(monkeypatch):
    monkeypatch.setattr(builder.mp, "Queue", MagicMock())
    monkeypatch.setattr(builder.mp, "Pipe", lambda **kwargs: (MagicMock(), MagicMock()))
    monkeypatch.setattr(builder, "AcquisitionWorker", MagicMock())
    monkeypatch.setattr(builder, "SignalProcessingWorker", MagicMock())

    def build(**settings):
        snapshot = {"CONFIG.revision": "a" * 64, "CONFIG.generation": "run-1", **settings}
        return builder.NeuroEngineBuilder(settings_snapshot=snapshot).build()
    return build


@pytest.mark.parametrize("stim_managed,actuation_managed", [
    (True, True), (False, True), (True, False), (False, False),
])
def test_each_enabled_listener_uses_its_own_external_identity(
    build_graph, stim_managed, actuation_managed,
):
    engine = build_graph(**{
        "STIMULATION.auto_spawn": stim_managed,
        "ACTUATION.auto_spawn": actuation_managed,
        "ACTUATION.enabled": True,
    })
    for role, managed in (("stimulation", stim_managed), ("actuation", actuation_managed)):
        server = engine.peer_gateway.peer(role)._transport._server
        expected = None if managed else PeerIdentity(role, "a" * 64, "run-1")
        assert server.identity == expected
        assert server.max_clients == (None if role == "stimulation" and managed else 1)


def test_managed_defaults_preserve_existing_listener_contract(build_graph):
    engine = build_graph(**{"ACTUATION.enabled": True})
    for role in ("stimulation", "actuation"):
        assert engine.peer_gateway.peer(role)._transport._server.identity is None


def test_disabled_external_actuation_does_not_create_listener_or_require_identity(build_graph):
    engine = build_graph(**{
        "ACTUATION.enabled": False, "ACTUATION.auto_spawn": False,
        "CONFIG.revision": None, "CONFIG.generation": None,
    })
    assert engine.peer_gateway.peer("stimulation")._transport._server.identity is None
    with pytest.raises(KeyError):
        engine.peer_gateway.peer("actuation")


@pytest.mark.parametrize("role", ["stimulation", "actuation"])
@pytest.mark.parametrize("missing", ["CONFIG.revision", "CONFIG.generation"])
def test_invalid_external_identity_fails_before_allocating_resources(build_graph, role, missing):
    snapshot = {f"{role.upper()}.auto_spawn": False, missing: None}
    if role == "actuation":
        snapshot["ACTUATION.enabled"] = True
    with pytest.raises(ValueError, match="Peer identity"):
        build_graph(**snapshot)
    builder.mp.Queue.assert_not_called()
    builder.AcquisitionWorker.assert_not_called()
    builder.SignalProcessingWorker.assert_not_called()


def test_peer_factories_preserve_routing_while_limiting_external_stimulation():
    identity = PeerIdentity("stimulation", "a" * 64, "run-1")
    managed = stimulation_peer_config("127.0.0.1", 8000)
    external = stimulation_peer_config("127.0.0.1", 8000, identity=identity)
    assert managed.routes == external.routes
    assert managed.transport._server.max_clients is None
    assert external.transport._server.max_clients == 1

    identity = PeerIdentity("actuation", "a" * 64, "run-1")
    managed = actuation_peer_config("127.0.0.1", 8001, watchdog_timeout_s=2)
    external = actuation_peer_config("127.0.0.1", 8001, watchdog_timeout_s=2, identity=identity)
    assert managed.routes == external.routes
    assert managed.transport._server.max_clients == external.transport._server.max_clients == 1
    assert managed.transport._server.idle_timeout == external.transport._server.idle_timeout == 4
