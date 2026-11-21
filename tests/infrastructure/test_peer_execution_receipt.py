"""A peer operation cannot reconnect with different execution evidence."""
import asyncio

import pytest

from omnibci.application.plugin_execution import PluginExecutionReceipt
from omnibci.infrastructure.ipc.contracts import encode_event
from omnibci.infrastructure.ipc.network import NetworkServer, read_event_frame
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.shared.events import Event, EventType


def receipt():
    return PluginExecutionReceipt(
        "actuation", "prepare-1", "run-1", "a" * 64, "b" * 64,
        "remote-node", "linux-aarch64", "c" * 64,
        {"actuation.effector": {"binding_sha256": "d" * 64, "content_sha256": "e" * 64,
                                "origin": "local", "verified_repository": None}},
    )


async def wait_closed_slot(server):
    async def poll():
        while server._clients or server._pending_clients or server._closing_writers:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 1)


@pytest.mark.parametrize("change", ["operation_id", "binding_sha256", "content_sha256",
                                    "environment_sha256", "node_id", "target"])
def test_accepted_operation_pins_actual_node_artifact_and_environment(change):
    async def scenario():
        evidence = receipt()
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
                await wait_closed_slot(server)

        try:
            # The coordinator does not compare a remote node's environment to its own.
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
        evidence = receipt()
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
        evidence = receipt()
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
