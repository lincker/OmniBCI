"""External peer sockets cannot participate before a pinned identity handshake."""

import asyncio

from omnibci.application.plugin_management.execution import PluginExecutionReceipt

import pytest

from omnibci.infrastructure.ipc.contracts import PROTOCOL_VERSION, encode_event
from omnibci.infrastructure.ipc import network, peer_transport
from omnibci.infrastructure.ipc.network import (
    NetworkClient, NetworkServer, StreamCleanupError, read_event_frame,
)
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.infrastructure.ipc.protocol import Frame
from omnibci.shared.events import Event, EventType


RECEIPT = PluginExecutionReceipt(
    "actuation", "prepare-1", "generation-1", "a" * 64, "c" * 64,
    "device-node", "linux-aarch64", "e" * 64,
    {"actuation.effector": {"binding_sha256": "d" * 64, "content_sha256": "f" * 64,
                            "origin": "local", "verified_repository": None}},
)
IDENTITY = PeerIdentity("actuation", "a" * 64, "c" * 64, "generation-1", "prepare-1", RECEIPT.bindings_sha256)


@pytest.mark.parametrize("keep_current", [False, True])
def test_old_configuration_identity_field_is_rejected(keep_current):
    payload = RECEIPT.to_dict()
    payload["system_revision"] = payload["engine_revision"]
    if not keep_current:
        del payload["engine_revision"]
    with pytest.raises(ValueError, match="missing or unknown"):
        PluginExecutionReceipt.from_dict(payload)


async def wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 1)


async def listen(**kwargs):
    server = NetworkServer(host="127.0.0.1", port=0, identity=IDENTITY, **kwargs)
    await server.start()
    return server, server.server.sockets[0].getsockname()[1]


def test_unverified_socket_cannot_receive_commands_or_claim_liveness():
    async def scenario():
        server, port = await listen()
        live = []
        server.set_liveness_handler(lambda: live.append(True))
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            await wait_for(lambda: server._pending_clients)
            assert not server.is_connected
            assert not await server.send_event(Event(EventType.ActuationCommand, {}))
            writer.write(encode_event(Event(EventType.ActuationState, {})))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert not server.is_connected
            assert live == []
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("field,value", [
    ("module", "stimulation"), ("module_revision", "b" * 64), ("generation", "generation-2"),
    ("engine_revision", "d" * 64), ("operation_id", "prepare-2"),
])
def test_wrong_identity_is_rejected(field, value):
    async def scenario():
        server, port = await listen()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            payload = RECEIPT.to_dict()
            payload[field] = value
            writer.write(encode_event(Event(EventType.PeerHello, payload)))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert not server.is_connected
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())


def test_matching_identity_ack_precedes_normal_bidirectional_traffic():
    async def scenario():
        server, port = await listen()
        inbound, outbound = [], []
        server.set_inbound_handler(lambda event: inbound.append(event) is None)
        client = NetworkClient(server_ip="127.0.0.1", server_port=port,
                               receipt=RECEIPT, reconnect=False)
        client.set_inbound_handler(lambda event: outbound.append(event) is None)
        task = asyncio.create_task(client.start())
        try:
            await wait_for(lambda: client.is_connected and server.is_connected)
            assert inbound == outbound == []
            assert await client.send_event(Event(EventType.ActuationState, {"ready": True}))
            assert await server.send_event(Event(EventType.ActuationCommand, {"target_id": 1}))
            await wait_for(lambda: inbound and outbound)
            assert inbound[0].type == EventType.ActuationState
            assert outbound[0].type == EventType.ActuationCommand
            await server.stop()
            await asyncio.wait_for(task, 1)
            assert not client.is_connected
        finally:
            await client.stop()
            await server.stop()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_pending_handshake_reserves_single_controller_slot_and_stop_closes_it():
    async def scenario():
        server, port = await listen(max_clients=1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        other_reader, other_writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            assert await asyncio.wait_for(other_reader.read(), 1) == b""
            assert not server.is_connected
            await server.stop()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert not server._pending_clients
            assert not server._closing_writers
        finally:
            writer.close()
            other_writer.close()
            await asyncio.gather(writer.wait_closed(), other_writer.wait_closed())
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["generation", "environment_sha256", "content_sha256"])
def test_client_requires_matching_ack_and_does_not_reconnect_old_identity(changed):
    async def scenario():
        connections = []
        async def stale_server(reader, writer):
            connections.append(True)
            hello = await read_event_frame(reader)
            assert hello.type == EventType.PeerHello
            payload = RECEIPT.to_dict()
            if changed == "content_sha256":
                payload["artifacts"]["actuation.effector"][changed] = "0" * 64
            else:
                payload[changed] = "0" * 64
            writer.write(encode_event(Event(EventType.PeerAccepted, payload)))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(stale_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = NetworkClient(server_port=port, receipt=RECEIPT, reconnect=False)
        try:
            with pytest.raises(ValueError, match="identity"):
                await asyncio.wait_for(client.start(), 1)
            assert not client.is_connected
            assert client.writer is None
            assert connections == [True]
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_keepalive_cannot_bypass_handshake():
    async def scenario():
        server, port = await listen()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(Frame("", version=PROTOCOL_VERSION).to_bytes())
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert not server.is_connected
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())


def test_handshake_timeout_releases_reserved_controller_slot(monkeypatch):
    monkeypatch.setattr(network, "PEER_HANDSHAKE_TIMEOUT", 0.02)
    async def scenario():
        server, port = await listen(max_clients=1)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            assert await asyncio.wait_for(reader.read(), 1) == b""
            await wait_for(lambda: not server._pending_clients and not server._closing_writers)
            client = NetworkClient(server_port=port, receipt=RECEIPT, reconnect=False)
            task = asyncio.create_task(client.start())
            try:
                await wait_for(lambda: client.is_connected)
                assert server.is_connected
            finally:
                await client.stop()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())


def test_client_cannot_send_before_ack_and_stop_closes_pending_handshake():
    async def scenario():
        hello_received, disconnected = asyncio.Event(), asyncio.Event()
        async def no_ack(reader, writer):
            hello = await read_event_frame(reader)
            assert hello.type == EventType.PeerHello
            hello_received.set()
            assert await reader.read() == b""
            writer.close()
            await writer.wait_closed()
            disconnected.set()
        server = await asyncio.start_server(no_ack, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = NetworkClient(server_port=port, receipt=RECEIPT, reconnect=False)
        task = asyncio.create_task(client.start())
        try:
            await asyncio.wait_for(hello_received.wait(), 1)
            assert not client.is_connected
            assert not await client.send_event(Event(EventType.ActuationState, {}))
            await client.stop()
            await asyncio.wait_for(disconnected.wait(), 1)
            assert client.writer is None
            assert not client._closing_writers
        finally:
            await client.stop()
            await asyncio.gather(task, return_exceptions=True)
            server.close()
            await server.wait_closed()
    asyncio.run(scenario())


def test_terminal_transport_notifies_owner_once_after_link_cleanup():
    async def scenario():
        server, port = await listen()
        ended = asyncio.Event()
        notifications = []
        def notify():
            notifications.append(transport._client.writer)
            ended.set()
        transport = peer_transport.ConnectTransport(
            "127.0.0.1", port, receipt=RECEIPT, reconnect=False, on_disconnect=notify,
        )
        try:
            await transport.start()
            await wait_for(lambda: transport.is_connected)
            await server.stop()
            await asyncio.wait_for(ended.wait(), 1)
            assert notifications == [None]
            assert not transport.is_connected
        finally:
            await transport.stop()
            await server.stop()
        assert notifications == [None]
    asyncio.run(scenario())


def test_deliberate_transport_stop_does_not_request_reconfiguration():
    async def scenario():
        server, port = await listen()
        notifications = []
        transport = peer_transport.ConnectTransport(
            "127.0.0.1", port, receipt=RECEIPT, reconnect=False,
            on_disconnect=lambda: notifications.append(True),
        )
        try:
            await transport.start()
            await wait_for(lambda: transport.is_connected)
            await transport.stop()
            assert notifications == []
            assert transport._client.writer is None
        finally:
            await transport.stop()
            await server.stop()
    asyncio.run(scenario())


def test_failed_pending_socket_cleanup_remains_owned_until_retry():
    class Writer:
        failure = True
        def get_extra_info(self, name):
            return "pending-peer"
        def close(self):
            pass
        async def wait_closed(self):
            if self.failure:
                raise RuntimeError("unconfirmed close")

    async def scenario():
        server = NetworkServer(identity=IDENTITY)
        writer = Writer()
        task = asyncio.create_task(server.handle_client(asyncio.StreamReader(), writer))
        await wait_for(lambda: server._pending_clients)
        with pytest.raises(StreamCleanupError):
            await server.stop()
        assert writer in server._closing_writers
        assert not server.is_connected
        writer.failure = False
        await server.stop()
        await asyncio.gather(task, return_exceptions=True)
        assert not server._pending_clients
        assert not server._closing_writers
    asyncio.run(scenario())


def test_terminal_client_does_not_retry_failed_initial_connection(monkeypatch):
    attempts = []
    async def refused(*args):
        attempts.append(True)
        raise ConnectionRefusedError()
    monkeypatch.setattr(network.asyncio, "open_connection", refused)
    async def scenario():
        client = NetworkClient(receipt=RECEIPT, reconnect=False)
        await asyncio.wait_for(client.start(), 1)
        assert attempts == [True]
        assert not client.is_connected
        assert client.writer is None
    asyncio.run(scenario())


def test_failed_handshake_cleanup_blocks_restart_until_socket_release(monkeypatch):
    class Writer:
        failure = True
        def write(self, data):
            pass
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            if self.failure:
                raise RuntimeError("unconfirmed close")

    async def scenario():
        writer = Writer()
        attempts = []
        reader = asyncio.StreamReader()
        reader.feed_data(encode_event(Event(EventType.PeerAccepted, {})))
        async def connect(*args):
            attempts.append(True)
            return reader, writer
        monkeypatch.setattr(network.asyncio, "open_connection", connect)
        ended = asyncio.Event()
        transport = peer_transport.ConnectTransport(
            "127.0.0.1", 8001, receipt=RECEIPT, reconnect=False,
            on_disconnect=ended.set,
        )
        await transport.start()
        await asyncio.wait_for(ended.wait(), 1)
        assert transport._client.writer is writer
        assert writer in transport._client._closing_writers
        with pytest.raises(StreamCleanupError):
            await transport.stop()
        with pytest.raises(StreamCleanupError):
            await transport.start()
        assert attempts == [True]
        assert transport._run_task is None
        writer.failure = False
        await transport.stop()
        assert transport._client.writer is None
        assert not transport._client._closing_writers
    asyncio.run(scenario())


@pytest.mark.parametrize("role,revision,generation", [
    ("source", "a" * 64, "generation"),
    ("stimulation", "revision", "generation"),
    ("actuation", "a" * 64, ""),
    ("actuation", "a" * 64, None),
])
def test_identity_requires_role_revision_and_generation(role, revision, generation):
    with pytest.raises(ValueError):
        PeerIdentity(role, revision, "c" * 64, generation, "prepare-1", RECEIPT.bindings_sha256)


def _exec_receipt():
    return PluginExecutionReceipt(
        "actuation", "prepare-1", "run-1", "a" * 64, "b" * 64,
        "remote-node", "linux-aarch64", "c" * 64,
        {"actuation.effector": {"binding_sha256": "d" * 64, "content_sha256": "e" * 64,
                                "origin": "local", "verified_repository": None}},
    )


async def _wait_closed_slot(server):
    async def poll():
        while server._clients or server._pending_clients or server._closing_writers:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 1)


@pytest.mark.parametrize("change", ["operation_id", "binding_sha256", "content_sha256",
                                    "environment_sha256", "node_id", "target"])
def test_accepted_operation_pins_actual_node_artifact_and_environment(change):
    async def scenario():
        evidence = _exec_receipt()
        identity = PeerIdentity("actuation", "a" * 64, "b" * 64, "run-1",
                                "prepare-1", evidence.bindings_sha256)
        server = NetworkServer("127.0.0.1", 0, max_clients=1, identity=identity)
        await server.start()
        port = server.server.sockets[0].getsockname()[1]

        async def exchange(data, accepted):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(encode_event(Event(EventType.PeerHello, data)))
                await writer.drain()
                if accepted:
                    event = await asyncio.wait_for(read_event_frame(reader), 1)
                    assert event.type == EventType.PeerAccepted
                    assert event.data == evidence.to_dict()
                    assert server.execution_receipt == evidence
                else:
                    assert await asyncio.wait_for(reader.read(), 1) == b""
                    assert not server.is_connected
            finally:
                writer.close()
                await writer.wait_closed()
                await _wait_closed_slot(server)

        try:
            await exchange(evidence.to_dict(), True)
            changed = evidence.to_dict()
            if change in {"binding_sha256", "content_sha256"}:
                changed["artifacts"]["actuation.effector"][change] = "f" * 64
            else:
                changed[change] = "f" * 64
            await exchange(changed, False)
            await exchange(evidence.to_dict(), True)
        finally:
            await server.stop()
    asyncio.run(scenario())


def test_first_handshake_rejects_extra_path_before_readiness():
    async def scenario():
        evidence = _exec_receipt()
        identity = PeerIdentity("actuation", "a" * 64, "b" * 64, "run-1",
                                "prepare-1", evidence.bindings_sha256)
        server = NetworkServer("127.0.0.1", 0, identity=identity)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
        try:
            data = evidence.to_dict()
            data["artifacts"]["actuation.effector"]["location"] = "/private/plugin"
            writer.write(encode_event(Event(EventType.PeerHello, data)))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert not server.is_connected
            assert server.execution_receipt is None
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())


def test_execution_history_failure_prevents_acknowledging_readiness():
    async def scenario():
        evidence = _exec_receipt()
        recorded = []

        def record(value):
            recorded.append(value)
            raise OSError("history unavailable")

        identity = PeerIdentity("actuation", "a" * 64, "b" * 64, "run-1",
                                "prepare-1", evidence.bindings_sha256)
        server = NetworkServer("127.0.0.1", 0, identity=identity, record_execution=record)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
        try:
            writer.write(encode_event(Event(EventType.PeerHello, evidence.to_dict())))
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 1) == b""
            assert recorded == [evidence]
            assert not server.is_connected
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_stop", [False, True])
def test_peer_stop_waits_for_history_write_even_when_cancelled(cancel_stop):
    import threading

    async def scenario():
        evidence = _exec_receipt()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()

        def record(value):
            assert value == evidence
            started.set()
            assert release.wait(2), "test did not release the history write"
            finished.set()

        identity = PeerIdentity("actuation", "a" * 64, "b" * 64, "run-1",
                                "prepare-1", evidence.bindings_sha256)
        server = NetworkServer("127.0.0.1", 0, identity=identity, record_execution=record)
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.server.sockets[0].getsockname()[1])
        stop = None
        try:
            writer.write(encode_event(Event(EventType.PeerHello, evidence.to_dict())))
            await writer.drain()
            assert await asyncio.to_thread(started.wait, 1)
            stop = asyncio.create_task(server.stop())
            await asyncio.sleep(.02)
            assert not stop.done()
            if cancel_stop:
                stop.cancel()
                await asyncio.sleep(.02)
                assert not stop.done()
            release.set()
            if cancel_stop:
                with pytest.raises(asyncio.CancelledError):
                    await stop
            else:
                await stop
            assert finished.is_set()
            assert not server.is_connected
        finally:
            release.set()
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            await server.stop()
    asyncio.run(scenario())
