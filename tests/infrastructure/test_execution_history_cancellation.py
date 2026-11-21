"""A stopped peer cannot abandon a history write still using its storage."""
import asyncio
import threading

import pytest

from omnibci.infrastructure.ipc.contracts import encode_event
from omnibci.infrastructure.ipc.network import NetworkServer
from omnibci.infrastructure.ipc.peer_identity import PeerIdentity
from omnibci.shared.events import Event, EventType
from tests.infrastructure.test_peer_execution_receipt import receipt


@pytest.mark.parametrize("cancel_stop", [False, True])
def test_peer_stop_waits_for_history_write_even_when_cancelled(cancel_stop):
    async def scenario():
        evidence = receipt()
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
