"""External nodes advertise only ready hardware and refetch only after cleanup."""
import asyncio
from importlib import import_module
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from tests.execution_helpers import synthetic_execution_snapshot


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
            try:
                self.setup()
                self.released = asyncio.Event()
                await self.released.wait()
            finally:
                self.teardown()

        def request_stop(self):
            if self.released is not None:
                self.released.set()

        def teardown(self):
            lifecycle.append("teardown")
            if self.released is not None:
                self.released.set()
            if cleanup_error:
                raise RuntimeError("hardware cleanup failed")

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
    snapshot = {"CONFIG.module_revisions": {'acquisition': "a" * 64, 'processing': "a" * 64, 'stimulation': "a" * 64, 'actuation': "a" * 64}, "CONFIG.engine_revision": "c" * 64, "CONFIG.generation": "run-1"}
    if external:
        snapshot["CONFIG.node_role"] = role
        snapshot = synthetic_execution_snapshot(snapshot)
    app = getattr(module, role.capitalize())(settings_snapshot=snapshot)
    return app, peer, peer_factory.call_args.args[0], lifecycle, queue


def test_managed_process_keeps_existing_transport_without_identity(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role, external=False)
    assert config.transport._client.receipt is None
    assert config.transport._client._reconnect is True
    assert app._external_node is False


def test_external_process_has_pinned_identity_and_single_connection(monkeypatch, role):
    app, peer, config, lifecycle, queue = make_app(monkeypatch, role)
    receipt = config.transport._client.receipt
    assert receipt.module == role
    assert receipt.module_revision == "a" * 64
    assert receipt.engine_revision == "c" * 64
    assert receipt.generation == "run-1"
    assert receipt.operation_id == "prepare-1"
    assert set(receipt.artifacts) == {"stimulation.paradigm" if role == "stimulation" else "actuation.effector"}
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
    from omnibci.infrastructure.actuation.actuator import Actuator
    from omnibci.infrastructure import node_client

    app, peer, config, lifecycle, queue = make_app(monkeypatch, "actuation")
    effector = Mock(setup=Mock(return_value=None), execute=Mock(return_value=None),
                    quiesce=Mock(return_value=None), teardown=Mock(return_value=None))
    getattr(effector, failure).side_effect = RuntimeError("device still held")
    actuator = Actuator(settings_snapshot={})
    actuator._load_effector = Mock(return_value=effector)
    app.actuator = actuator
    peer.start.side_effect = None

    async def disconnected(*_):
        app._connection_lost()
        raise RuntimeError("transport ended")

    peer.wait_until_connected.side_effect = disconnected
    fetch = Mock(side_effect=[Mock(module_revisions={"actuation": "a" * 64}, engine_revision="c" * 64, generation="run-1"),
                              AssertionError("unsafe retry after failed device cleanup")])
    factory = Mock(return_value=app)
    sleep = Mock()
    monkeypatch.setattr(node_client, "fetch_node_configuration", fetch)
    monkeypatch.setattr(node_client, "materialize_node_configuration", Mock(return_value={"PLUGINS.bindings": []}))
    monkeypatch.setattr(node_client, "CatalogConfigurationAdapter", Mock())
    monkeypatch.setattr(node_client.time, "sleep", sleep)

    with pytest.raises(RuntimeError, match="device still held"):
        node_client.run_configured_node("actuation", factory, server_url="http://server")
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


@pytest.mark.parametrize("node_kind", ["Stimulation", "Actuation"])
@pytest.mark.parametrize("peer_name,channel_id,stopped", [
    ("engine", "current", True),
    ("engine", "replaced", False),
    ("another", "current", False),
])
def test_only_current_engine_channel_fault_stops_node(node_kind, peer_name, channel_id, stopped):
    from unittest.mock import MagicMock
    from omnibci.infrastructure.actuation.application import Actuation
    from omnibci.infrastructure.stimulation.application import Stimulation
    from omnibci.shared.events import Event, EventType

    node_class = Stimulation if node_kind == "Stimulation" else Actuation
    app = node_class.__new__(node_class)
    app.event_bus = MagicMock(start=AsyncMock())
    app.peer_channel = MagicMock(channel_id="current", start=AsyncMock())
    app.actuator = MagicMock()
    app._external_node = True
    app._reconnect_requested = False
    app._paused = False
    app.should_stop = False
    if node_class is Actuation:
        app._device_executor = None
    try:
        asyncio.run(app.start())
    finally:
        if node_class is Actuation and app._device_executor is not None:
            app._device_executor.shutdown(wait=True)
    subscriptions = {
        call.kwargs["event_type"]: call.kwargs["handler"]
        for call in app.event_bus.register_event_listener.call_args_list
    }

    subscriptions[EventType.PeerDeliveryFault](Event(EventType.PeerDeliveryFault, {
        "peer": peer_name, "channel_id": channel_id,
        "reason": "Sender queue is full", "unsent_command_ids": [],
    }))

    assert app.should_stop is stopped
    assert app._paused is stopped
    assert app._reconnect_requested is stopped


def _make_trig_app(monkeypatch, *, clear_error=False, render_error=False, setup_error=False,
                   initialize_trigger=True, external=True):
    from omnibci.infrastructure.stimulation.application import Stimulation

    snapshot = {"CONFIG.node_role": "stimulation", "CONFIG.module_revisions": {"stimulation": "1" * 64},
                "CONFIG.engine_revision": "2" * 64, "CONFIG.generation": "generation"} if external else {}
    if external:
        snapshot = synthetic_execution_snapshot(snapshot)
    app = Stimulation(settings_snapshot=snapshot)
    steps = []

    class Trigger:
        def setup(self):
            if not initialize_trigger:
                raise RuntimeError("trigger initialization failed")
        def clear(self):
            steps.append("trigger-clear")
            if clear_error:
                raise RuntimeError("trigger release unconfirmed")
        def teardown(self):
            steps.append("trigger-teardown")

    class Renderer:
        def initialize(self, **kwargs):
            pass
        def should_close(self):
            return False
        def poll_events(self):
            pass
        def begin_frame(self):
            return None
        def end_frame(self, frame, *, submit):
            pass
        def shutdown(self):
            steps.append("renderer-shutdown")

    class Paradigm:
        def setup(self, *, renderer, trigger):
            if setup_error:
                raise RuntimeError("paradigm initialization failed")
        def render_frame(self, frame):
            return False
        def teardown(self):
            steps.append("paradigm-teardown")
            if render_error:
                raise RuntimeError("renderer release unconfirmed")

    async def ready():
        if external:
            app._connection_lost()
        else:
            app.should_stop = True

    monkeypatch.setattr(app.stimulator, "_load_trigger", lambda: Trigger())
    monkeypatch.setattr(app.stimulator, "_load_paradigm", lambda: Paradigm())
    monkeypatch.setattr("omnibci.infrastructure.stimulation.stimulator._create_renderer", lambda name: Renderer())
    monkeypatch.setattr(app, "start", AsyncMock())
    monkeypatch.setattr(app, "_await_managed_readiness", ready)

    async def close():
        steps.append("ipc-stop")
        app._close_event_queue()

    monkeypatch.setattr(app, "stop", close)
    return app, steps


def test_reconnect_requires_stopped_renderer_and_cleared_trigger(monkeypatch):
    app, steps = _make_trig_app(monkeypatch)
    assert app.run() is True
    assert steps == ["trigger-clear", "paradigm-teardown", "trigger-clear", "trigger-teardown", "renderer-shutdown", "ipc-stop"]


def test_trigger_clear_failure_is_fatal_and_still_closes_ipc(monkeypatch):
    app, steps = _make_trig_app(monkeypatch, clear_error=True)
    with pytest.raises(ExceptionGroup, match="cleanup also failed"):
        app.run()
    assert steps == ["trigger-clear", "trigger-clear", "trigger-teardown", "renderer-shutdown", "ipc-stop"]


def test_render_teardown_failure_still_clears_trigger_and_closes_renderer(monkeypatch):
    app, steps = _make_trig_app(monkeypatch, render_error=True)
    with pytest.raises(RuntimeError, match="renderer release unconfirmed"):
        app.run()
    assert steps == ["trigger-clear", "paradigm-teardown", "trigger-clear", "trigger-teardown", "renderer-shutdown", "ipc-stop"]


@pytest.mark.parametrize("initialize_trigger", [False, True])
def test_failed_setup_clears_only_a_successfully_initialized_trigger(monkeypatch, initialize_trigger):
    app, steps = _make_trig_app(monkeypatch, setup_error=True, initialize_trigger=initialize_trigger)
    with pytest.raises(RuntimeError, match="initialization failed"):
        app.run()
    assert steps == (["trigger-clear", "paradigm-teardown", "trigger-clear"] if initialize_trigger else []) + [
        "trigger-teardown", "renderer-shutdown", "ipc-stop"]


def test_managed_shutdown_uses_same_trigger_lifecycle(monkeypatch):
    app, steps = _make_trig_app(monkeypatch, external=False)
    assert app.run() is False
    assert steps == ["trigger-clear", "paradigm-teardown", "trigger-clear", "trigger-teardown", "renderer-shutdown", "ipc-stop"]


def _cleanup_application(kind: str):
    from unittest.mock import MagicMock
    from omnibci.infrastructure.actuation.application import Actuation
    from omnibci.infrastructure.stimulation.application import Stimulation

    application_type = Actuation if kind == "actuation" else Stimulation
    application = application_type.__new__(application_type)
    application.peer_channel = MagicMock()
    application.peer_channel.stop = AsyncMock()
    application.event_bus = MagicMock()
    application.event_bus.stop = AsyncMock()
    application._close_event_queue = MagicMock()
    return application


def _set_cleanup_failure(application, site: str, error: BaseException) -> None:
    if site == "peer_channel":
        application.peer_channel.stop.side_effect = error
    elif site == "event_bus":
        application.event_bus.stop.side_effect = error
    else:
        application._close_event_queue.side_effect = error


@pytest.mark.parametrize("application_kind", ["actuation", "stimulation"])
class TestApplicationCleanup:
    @pytest.mark.asyncio
    async def test_success_releases_every_ipc_owner(self, application_kind):
        application = _cleanup_application(application_kind)
        await application.stop()
        application.peer_channel.stop.assert_awaited_once_with()
        application.event_bus.stop.assert_awaited_once_with()
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_site", ["peer_channel", "event_bus", "event_queue"])
    async def test_component_failure_is_aggregated_after_remaining_cleanup(self, application_kind, failure_site):
        application = _cleanup_application(application_kind)
        error = OSError(f"{failure_site} failed")
        _set_cleanup_failure(application, failure_site, error)
        with pytest.raises(ExceptionGroup) as raised:
            await application.stop()
        assert raised.value.exceptions == (error,)
        application.peer_channel.stop.assert_awaited_once_with()
        application.event_bus.stop.assert_awaited_once_with()
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cancel_site", ["peer_channel", "event_bus"])
    async def test_cancellation_is_re_raised_after_remaining_cleanup(self, application_kind, cancel_site):
        application = _cleanup_application(application_kind)
        cancellation = asyncio.CancelledError(f"{cancel_site} cancelled")
        _set_cleanup_failure(application, cancel_site, cancellation)
        with pytest.raises(asyncio.CancelledError) as raised:
            await application.stop()
        assert raised.value is cancellation
        assert raised.value.__cause__ is None
        application.peer_channel.stop.assert_awaited_once_with()
        application.event_bus.stop.assert_awaited_once_with()
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_first_cancellation_wins_when_both_async_owners_cancel(self, application_kind):
        application = _cleanup_application(application_kind)
        peer_cancellation = asyncio.CancelledError("peer cancelled")
        bus_cancellation = asyncio.CancelledError("bus cancelled")
        application.peer_channel.stop.side_effect = peer_cancellation
        application.event_bus.stop.side_effect = bus_cancellation
        with pytest.raises(asyncio.CancelledError) as raised:
            await application.stop()
        assert raised.value is peer_cancellation
        application._close_event_queue.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_cancellation_preserves_additional_cleanup_errors_as_cause(self, application_kind):
        application = _cleanup_application(application_kind)
        cancellation = asyncio.CancelledError("peer cancelled")
        cleanup_error = OSError("event bus failed")
        application.peer_channel.stop.side_effect = cancellation
        application.event_bus.stop.side_effect = cleanup_error
        with pytest.raises(asyncio.CancelledError) as raised:
            await application.stop()
        assert raised.value is cancellation
        assert isinstance(raised.value.__cause__, ExceptionGroup)
        assert raised.value.__cause__.exceptions == (cleanup_error,)
        application._close_event_queue.assert_called_once_with()


@pytest.mark.asyncio
async def test_source_rejects_invalid_payload_before_connecting():
    from types import SimpleNamespace
    from omnibci.application.events import CommandRequest
    from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand, AcquisitionRuntime
    from tests.infrastructure.test_acquisition_disconnect import ContractSource

    source = ContractSource()
    owner = AcquisitionRuntime(source, Mock(), Mock(), source_id="source")
    events = []
    owner.event_bus = SimpleNamespace(put_event=events.append)
    await owner.execute((AcquisitionCommand.connect, CommandRequest("connect", {"ignored": True})))
    assert not source.calls
    assert not events[-1].data.ok
    assert events[-1].data.error_code == "INVALID_COMMAND_PAYLOAD"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name,method", [
    ("SettleActuation", "_request_settlement"),
    ("ResumeActuation", "_request_resume"),
])
async def test_actuation_rejects_invalid_payload_before_reserving_device(event_name, method):
    from omnibci.application.events import CommandRequest
    from omnibci.shared.events import Event, EventType
    from tests.actuation.test_application import _app

    app = _app()
    app._settle_actuation = AsyncMock()
    app._resume_actuation = AsyncMock()
    getattr(app, method)(Event(EventType[event_name], CommandRequest("control", {"ignored": True})))
    await asyncio.sleep(0)
    assert not app._paused
    app._settle_actuation.assert_not_called()
    app._resume_actuation.assert_not_called()
    assert app.event_bus.events[-1].data.error_code == "INVALID_COMMAND_PAYLOAD"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name,method", [
    ("PauseStimulation", "_pause_stimulation"),
    ("StopStimulation", "_stop_stimulation_system"),
])
async def test_stimulation_rejects_invalid_payload_before_stopping(event_name, method):
    from queue import Queue
    from types import SimpleNamespace
    from omnibci.application.events import CommandRequest
    from omnibci.infrastructure.stimulation.application import Stimulation
    from omnibci.shared.events import Event, EventType

    app = Stimulation.__new__(Stimulation)
    events = []
    app.event_bus = SimpleNamespace(put_event=events.append)
    app._paused = app.should_stop = False
    app._stop_requests = []
    app.stimulator = Mock()
    app.stim_command_queue = Queue()
    app.result_command_queue = Queue()
    app._render_task = asyncio.create_task(asyncio.sleep(0))
    try:
        await getattr(app, method)(Event(EventType[event_name], CommandRequest("control", {"ignored": True})))
        assert not app._paused and not app.should_stop
        app.stimulator.request_stop.assert_not_called()
        assert events[-1].data.error_code == "INVALID_COMMAND_PAYLOAD"
    finally:
        await app._render_task
