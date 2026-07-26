"""Lifecycle contract tests for the public peer transport adapters."""

from __future__ import annotations

import asyncio

import pytest

from omnibci.infrastructure.ipc import peer_transport
from omnibci.shared.events import Event, EventType


class _FakeServer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.start_calls = 0
        self.stop_calls = 0
        self.send_calls = []
        self.is_connected = False
        self.start_entered = asyncio.Event()
        self.start_release = asyncio.Event()
        self.start_error = None
        self.stop_error = None

    async def start(self):
        self.start_calls += 1
        self.start_entered.set()
        await self.start_release.wait()
        if self.start_error is not None:
            raise self.start_error

    async def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error

    async def send_event(self, event):
        self.send_calls.append(event)
        return self.is_connected


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.server_ip = kwargs["server_ip"]
        self.server_port = kwargs["server_port"]
        self.start_calls = 0
        self.stop_calls = 0
        self.send_calls = []
        self.is_connected = False
        self.start_entered = asyncio.Event()
        self.run_forever = asyncio.Event()
        self.start_error = None
        self.stop_error = None
        self.writer = None
        self._closing_writers = set()

    async def start(self):
        self.start_calls += 1
        self.start_entered.set()
        if self.start_error is not None:
            raise self.start_error
        await self.run_forever.wait()

    async def stop(self):
        self.stop_calls += 1
        if self.stop_error is not None:
            raise self.stop_error
        self.writer = None
        self._closing_writers.clear()

    async def send_event(self, event):
        self.send_calls.append(event)
        return self.is_connected


class TestListenTransport:
    def test_liveness_handler_is_part_of_abstract_transport_contract(self):
        assert "set_liveness_handler" in peer_transport.PeerTransport.__abstractmethods__

    def test_start_awaits_bind_and_lifecycle_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(peer_transport, "NetworkServer", _FakeServer)
        transport = peer_transport.ListenTransport("127.0.0.1", 8001)

        async def scenario():
            start_task = asyncio.create_task(transport.start())
            await transport._server.start_entered.wait()
            assert start_task.done() is False
            transport._server.start_release.set()
            await start_task

            await transport.start()
            assert transport._server.start_calls == 1
            assert not hasattr(transport, "_serve_task")

            await transport.stop()
            await transport.stop()
            assert transport._server.stop_calls == 1

        asyncio.run(scenario())

    def test_failed_start_is_cleaned_and_can_be_retried(self, monkeypatch):
        monkeypatch.setattr(peer_transport, "NetworkServer", _FakeServer)
        transport = peer_transport.ListenTransport("127.0.0.1", 8001)
        transport._server.start_error = RuntimeError("bind failed")
        transport._server.start_release.set()

        async def scenario():
            with pytest.raises(RuntimeError, match="bind failed"):
                await transport.start()
            assert transport._server.stop_calls == 1

            transport._server.start_error = None
            await transport.start()
            await transport.stop()

        asyncio.run(scenario())
        assert transport._server.start_calls == 2
        assert transport._server.stop_calls == 2

    def test_failed_start_with_failed_rollback_retains_cleanup_obligation(
        self, monkeypatch
    ):
        monkeypatch.setattr(peer_transport, "NetworkServer", _FakeServer)
        transport = peer_transport.ListenTransport("127.0.0.1", 8001)
        transport._server.start_error = RuntimeError("bind failed")
        transport._server.stop_error = RuntimeError("listener cleanup failed")
        transport._server.start_release.set()

        async def scenario():
            with pytest.raises(RuntimeError, match="listener cleanup failed"):
                await transport.start()
            assert transport._started is True

            transport._server.stop_error = None
            await transport.stop()
            assert transport._started is False

            transport._server.start_error = None
            await transport.start()
            await transport.stop()

        asyncio.run(scenario())
        assert transport._server.start_calls == 2
        assert transport._server.stop_calls == 3

    def test_timeouts_are_forwarded_and_send_returns_delivery_state(self, monkeypatch):
        monkeypatch.setattr(peer_transport, "NetworkServer", _FakeServer)
        transport = peer_transport.ListenTransport(
            "127.0.0.1",
            8001,
            max_clients=1,
            idle_timeout=2.0,
            write_timeout=0.25,
        )
        assert transport._server.kwargs == {
            "event_bus": None,
            "host": "127.0.0.1",
            "port": 8001,
            "max_clients": 1,
            "idle_timeout": 2.0,
            "write_timeout": 0.25,
        }

        event = Event(EventType.ActuationCommand, {"target_id": 1})
        assert asyncio.run(transport.send(event)) is False
        transport._server.is_connected = True
        assert transport.is_connected is True
        assert asyncio.run(transport.send(event)) is True


class TestConnectTransport:
    def test_exactly_one_supervisor_and_idempotent_stop(self, monkeypatch):
        monkeypatch.setattr(peer_transport, "NetworkClient", _FakeClient)
        transport = peer_transport.ConnectTransport("127.0.0.1", 8001)

        async def scenario():
            await transport.start()
            task = transport._run_task
            await transport._client.start_entered.wait()
            await transport.start()
            assert transport._run_task is task
            assert transport._client.start_calls == 1

            await transport.stop()
            assert task.done()
            assert transport._client.stop_calls == 1
            await transport.stop()
            assert transport._client.stop_calls == 1

        asyncio.run(scenario())

    def test_start_fails_synchronously_until_pending_client_cleanup_succeeds(
        self, monkeypatch
    ):
        monkeypatch.setattr(peer_transport, "NetworkClient", _FakeClient)
        transport = peer_transport.ConnectTransport("127.0.0.1", 8001)
        transport._client.writer = object()
        transport._client._closing_writers.add(transport._client.writer)
        transport._client.stop_error = RuntimeError("socket cleanup failed")

        async def scenario():
            with pytest.raises(RuntimeError, match="socket cleanup failed"):
                await transport.start()
            assert transport._run_task is None
            assert transport._client.start_calls == 0

            transport._client.stop_error = None
            await transport.start()
            await transport._client.start_entered.wait()
            assert transport._client.start_calls == 1
            await transport.stop()

        asyncio.run(scenario())

    def test_supervisor_exception_is_harvested_and_client_is_closed(self, monkeypatch):
        monkeypatch.setattr(peer_transport, "NetworkClient", _FakeClient)
        transport = peer_transport.ConnectTransport("127.0.0.1", 8001)
        transport._client.start_error = RuntimeError("receive loop crashed")

        async def scenario():
            await transport.start()
            task = transport._run_task
            with pytest.raises(RuntimeError, match="receive loop crashed"):
                await task
            await asyncio.sleep(0)  # let the done callback harvest the result
            assert isinstance(transport._last_supervisor_error, RuntimeError)
            assert transport._client.stop_calls == 1
            await transport.stop()  # a completed failed task is still collected

        asyncio.run(scenario())

    def test_stop_caller_cancellation_waits_for_cleanup_and_preserves_it(
        self, monkeypatch
    ):
        class _SlowStopClient(_FakeClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.stop_entered = asyncio.Event()
                self.stop_release = asyncio.Event()

            async def stop(self):
                self.stop_calls += 1
                self.stop_entered.set()
                await self.stop_release.wait()

        monkeypatch.setattr(peer_transport, "NetworkClient", _SlowStopClient)
        transport = peer_transport.ConnectTransport("127.0.0.1", 8001)

        async def scenario():
            await transport.start()
            supervisor = transport._run_task
            await transport._client.start_entered.wait()

            stop_task = asyncio.create_task(transport.stop())
            await transport._client.stop_entered.wait()
            stop_task.cancel()
            await asyncio.sleep(0)

            assert not supervisor.done()
            assert transport._run_task is supervisor
            assert not stop_task.done()

            transport._client.stop_release.set()
            with pytest.raises(asyncio.CancelledError):
                await stop_task

            assert supervisor.done()
            assert transport._run_task is None
            assert transport._client.stop_calls == 1

        asyncio.run(scenario())

    def test_timeouts_are_forwarded_and_send_returns_delivery_state(self, monkeypatch):
        monkeypatch.setattr(peer_transport, "NetworkClient", _FakeClient)
        transport = peer_transport.ConnectTransport(
            "10.0.0.2",
            9001,
            connect_timeout=0.8,
            reconnect_initial=0.2,
            reconnect_max=3.0,
            idle_timeout=2.0,
            write_timeout=0.25,
        )
        assert transport._client.kwargs == {
            "event_bus": None,
            "server_ip": "10.0.0.2",
            "server_port": 9001,
            "connect_timeout": 0.8,
            "reconnect_initial": 0.2,
            "reconnect_max": 3.0,
            "idle_timeout": 2.0,
            "write_timeout": 0.25,
        }

        event = Event(EventType.ActuationAck, {"status": "ok"})
        assert asyncio.run(transport.send(event)) is False
        transport._client.is_connected = True
        assert transport.is_connected is True
        assert asyncio.run(transport.send(event)) is True
