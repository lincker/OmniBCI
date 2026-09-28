"""External nodes advertise only ready hardware and refetch only after cleanup."""
import asyncio
from importlib import import_module
import threading
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture(params=["stimulation", "actuation"])
def role(request):
    return request.param


def make_app(monkeypatch, role, *, external=True, setup_error=False, cleanup_error=False):
    module = import_module(f"omnibci.infrastructure.{role}.application")
    lifecycle = []

    class Driver:
        ready = False
        released = None

        def setup(self):
            lifecycle.append("setup")
            if setup_error:
                raise RuntimeError("hardware setup failed")
            self.ready = True

        async def start_async(self):
            self.setup()
            self.released = asyncio.Event()
            await self.released.wait()

        def teardown(self):
            lifecycle.append("teardown")
            if self.released is not None:
                self.released.set()
            if cleanup_error:
                raise RuntimeError("hardware cleanup failed")

        stop = teardown

        def quiesce(self):
            lifecycle.append("quiesce")

    driver = Driver()
    queue = Mock()
    bus = Mock(start=AsyncMock(), stop=AsyncMock())
    peer = Mock()

    async def start_peer():
        assert driver.ready
        lifecycle.append("handshake")

    async def stop_peer():
        lifecycle.append("ipc-stop")

    peer.start = AsyncMock(side_effect=start_peer)
    peer.stop = AsyncMock(side_effect=stop_peer)
    peer.wait_until_connected = AsyncMock()
    peer_factory = Mock(return_value=peer)
    monkeypatch.setattr(module.mp, "Queue", Mock(return_value=queue))
    monkeypatch.setattr(module, "EventBus", Mock(return_value=bus))
    monkeypatch.setattr(module, "PeerChannel", peer_factory)
    monkeypatch.setattr(module, "Stimulator" if role == "stimulation" else "Actuator",
                        Mock(return_value=driver))
    snapshot = {"CONFIG.revision": "a" * 64, "CONFIG.generation": "run-1"}
    if external:
        snapshot["CONFIG.node_role"] = role
    app = getattr(module, role.capitalize())(settings_snapshot=snapshot)
    return app, peer, peer_factory.call_args.args[0], lifecycle, queue


def test_managed_process_keeps_existing_transport_without_identity(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role, external=False)
    assert config.transport._client.identity is None
    assert config.transport._client._reconnect is True
    assert app._external_node is False


def test_external_process_has_pinned_identity_and_single_connection(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role)
    assert config.transport._client.identity.to_dict() == {
        "role": role, "config_revision": "a" * 64, "generation": "run-1",
    }
    assert config.transport._client._reconnect is False


@pytest.mark.parametrize("during_startup", [False, True])
def test_clean_connection_loss_closes_hardware_before_requesting_refetch(monkeypatch, role, during_startup):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role)

    async def lose_connection(*_):
        app._connection_lost()
        if during_startup:
            raise RuntimeError("transport ended")

    if during_startup:
        peer.wait_until_connected.side_effect = lose_connection
    else:
        app.execute_command = AsyncMock(side_effect=lose_connection)
    assert app.run() is True
    assert lifecycle == ["setup", "handshake", *(["quiesce"] if role == "actuation" else []),
                         "teardown", "ipc-stop"]
    assert app._paused is True
    queue.close.assert_called_once()
    peer.wait_until_connected.assert_awaited_once_with(10.0)


def test_hardware_setup_failure_is_fatal_and_does_not_advertise_connection(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role, setup_error=True)
    with pytest.raises(RuntimeError, match="hardware setup failed"):
        app.run()
    peer.start.assert_not_awaited()
    peer.stop.assert_awaited_once()
    assert "teardown" in lifecycle


def test_hardware_cleanup_failure_never_returns_reconnect_marker(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role, cleanup_error=True)

    async def disconnected(*_):
        app._connection_lost()
        raise RuntimeError("transport ended")

    peer.wait_until_connected.side_effect = disconnected
    with pytest.raises(RuntimeError, match="hardware cleanup failed"):
        app.run()
    peer.stop.assert_awaited_once()


def test_ipc_cleanup_failure_never_returns_reconnect_marker(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role)

    async def disconnected(*_):
        app._connection_lost()
        raise RuntimeError("transport ended")

    peer.wait_until_connected.side_effect = disconnected
    peer.stop.side_effect = RuntimeError("socket close failed")
    with pytest.raises((RuntimeError, ExceptionGroup), match="socket close failed|cleanup failed"):
        app.run()
    assert "teardown" in lifecycle


def test_unconfirmed_connection_failure_is_fatal(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role)
    peer.wait_until_connected.side_effect = TimeoutError("connection unconfirmed")
    with pytest.raises(TimeoutError, match="connection unconfirmed"):
        app.run()
    assert "teardown" in lifecycle


def test_lost_link_stops_commands_before_delayed_transport_cleanup(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role)
    peer.is_connected = False
    if role == "actuation":
        app.command_queue = Mock()
    else:
        app.stim_command_queue = Mock()
        app.result_command_queue = Mock()
    asyncio.run(app.execute_command())
    assert app._reconnect_requested is True
    assert app.should_stop is True
    if role == "actuation":
        app.command_queue.get_nowait.assert_not_called()
    else:
        app.stim_command_queue.get.assert_not_called()
        app.result_command_queue.get.assert_not_called()


@pytest.mark.parametrize("failure", ["quiesce", "teardown"])
def test_real_effector_failed_cleanup_cannot_restart_from_supervisor(monkeypatch, failure):
    import omnibci.conf as conf
    from omnibci.infrastructure.actuation.actuator import Actuator
    from omnibci.infrastructure.plugins import node_client

    app, peer, config, lifecycle, queue = make_app(monkeypatch, "actuation")
    effector = Mock()
    getattr(effector, failure).side_effect = RuntimeError("device still held")
    actuator = Actuator()
    actuator._load_effector = Mock(return_value=effector)
    app.actuator = actuator
    peer.start.side_effect = None

    async def disconnected(*_):
        app._connection_lost()
        raise RuntimeError("transport ended")

    peer.wait_until_connected.side_effect = disconnected
    fetch = Mock(side_effect=[Mock(revision="a" * 64, generation="run-1"),
                              AssertionError("unsafe retry after failed device cleanup")])
    factory = Mock(return_value=app)
    sleep = Mock()
    monkeypatch.setattr(node_client, "fetch_node_configuration", fetch)
    monkeypatch.setattr(node_client, "materialize_node_configuration", Mock(return_value={}))
    monkeypatch.setattr(node_client, "CatalogConfigurationAdapter", Mock())
    monkeypatch.setattr(node_client.time, "sleep", sleep)
    monkeypatch.setattr(conf, "settings", Mock())

    with pytest.raises(RuntimeError, match="device still held"):
        node_client.run_configured_node("actuation", factory, backend_url="http://backend")
    fetch.assert_called_once()
    factory.assert_called_once()
    sleep.assert_not_called()
    peer.stop.assert_awaited_once()
    effector.quiesce.assert_called_once()
    effector.teardown.assert_called_once()
    assert actuator.effector is (effector if failure == "teardown" else None)


def test_cancelled_quiescence_finishes_physical_work_before_teardown(monkeypatch):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, "actuation")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    teardown_states = []

    def quiesce():
        started.set()
        if not release.wait(timeout=2):
            raise RuntimeError("test did not release physical operation")
        finished.set()

    app.actuator.quiesce = quiesce
    app.actuator.teardown = lambda: teardown_states.append(finished.is_set())

    async def disconnected(*_):
        app._connection_lost()
        raise RuntimeError("transport ended")

    peer.wait_until_connected.side_effect = disconnected

    async def scenario():
        task = asyncio.create_task(app.serve())
        try:
            async with asyncio.timeout(1):
                while not started.is_set():
                    await asyncio.sleep(0.001)
            task.cancel()
            await asyncio.sleep(0.01)
            assert teardown_states == []
            peer.stop.assert_not_awaited()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert teardown_states == [True]
    peer.stop.assert_awaited_once()
