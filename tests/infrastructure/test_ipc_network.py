"""
Tests for the stimulation IPC socket channel (NetworkServer ↔ NetworkClient).

This channel had no test coverage, which is why several bugs lived in it:
  - server never json.loads'd the body and read the wrong key (event_type);
  - client.send_event called a non-existent Event.to_dict();
  - client.stop called a non-existent writer.disconnect().

These tests pin the fixed behaviour:
  1. Frame length-prefix codec round-trips (pure, no sockets).
  2. A real loopback TCP round-trip delivers events in BOTH directions and a
     client disconnect does not crash the server handler.
  3. ClientMessageManagement keeps a strong reference to its background task.

Round-trips use a real asyncio server on 127.0.0.1 with an ephemeral port —
no external services, no settings, no multiprocessing.
"""
from __future__ import annotations

import asyncio

import pytest

from omnibci.infrastructure.ipc.protocol import Frame
from omnibci.infrastructure.ipc.network import NetworkServer, NetworkClient
from omnibci.shared.events import Event, EventType


# ---------------------------------------------------------------------------
# 1. Frame codec — pure
# ---------------------------------------------------------------------------

class TestFrameCodec:
    def test_header_size_is_eight(self):
        assert Frame.Header.HEADER_SIZE == 8

    def test_header_roundtrip(self):
        hdr = Frame.Header(version=1, body_size=123)
        back = Frame.Header.from_bytes(hdr.to_bytes())
        assert (back.version, back.body_size) == (1, 123)

    def test_frame_body_size_counts_bytes_not_chars(self):
        # Multi-byte body: header body_size must equal UTF-8 byte length.
        body = "结果"  # 2 chars, 6 UTF-8 bytes
        frame = Frame(body).to_bytes()
        header = Frame.Header.from_bytes(frame[: Frame.Header.HEADER_SIZE])
        assert header.body_size == len(body.encode())
        assert frame[Frame.Header.HEADER_SIZE:].decode() == body


# ---------------------------------------------------------------------------
# Spy event bus — captures put_event calls (sync, like the real bus seam)
# ---------------------------------------------------------------------------

class _SpyBus:
    def __init__(self) -> None:
        self.events: list = []

    def put_event(self, event) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# 2. Loopback round-trip over real TCP
# ---------------------------------------------------------------------------

async def _serve_one(server: NetworkServer):
    srv = await asyncio.start_server(server.handle_client, host="127.0.0.1", port=0)
    server.server = srv
    port = srv.sockets[0].getsockname()[1]
    return srv, port


class TestLoopbackRoundTrip:
    def test_server_to_client_delivers_event(self):
        async def scenario():
            server_bus, client_bus = _SpyBus(), _SpyBus()
            server = NetworkServer(event_bus=server_bus, host="127.0.0.1", port=0)
            srv, port = await _serve_one(server)
            client = NetworkClient(event_bus=client_bus, server_ip="127.0.0.1", server_port=port)
            client.reader, client.writer = await asyncio.open_connection("127.0.0.1", port)

            await asyncio.sleep(0.05)  # let server accept + set self.writer
            await server.send_event(Event(EventType.TrialCompleted, data=7))

            await client.receive()  # read one frame
            assert len(client_bus.events) == 1
            assert client_bus.events[0].type == EventType.TrialCompleted
            assert client_bus.events[0].data == 7

            await client.stop()
            srv.close()
        asyncio.run(scenario())

    def test_client_to_server_delivers_event(self):
        """The previously-broken direction (server missed json.loads + key)."""
        async def scenario():
            server_bus, client_bus = _SpyBus(), _SpyBus()
            server = NetworkServer(event_bus=server_bus, host="127.0.0.1", port=0)
            srv, port = await _serve_one(server)
            client = NetworkClient(event_bus=client_bus, server_ip="127.0.0.1", server_port=port)
            client.reader, client.writer = await asyncio.open_connection("127.0.0.1", port)

            await asyncio.sleep(0.05)
            await client.send_event(Event(EventType.StartStimTrial, data=3))
            await asyncio.sleep(0.05)  # let server handler read + dispatch

            assert any(
                e.type == EventType.StartStimTrial and e.data == 3
                for e in server_bus.events
            )
            await client.stop()
            srv.close()
        asyncio.run(scenario())

    def test_client_disconnect_does_not_crash_server_handler(self):
        async def scenario():
            server_bus = _SpyBus()
            server = NetworkServer(event_bus=server_bus, host="127.0.0.1", port=0)
            srv, port = await _serve_one(server)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            assert len(server._clients) == 1
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)  # server handler hits IncompleteReadError
            # handler caught the disconnect and removed the client
            assert len(server._clients) == 0
            srv.close()
        asyncio.run(scenario())

    def test_send_event_broadcasts_to_all_clients(self):
        async def scenario():
            server = NetworkServer(event_bus=_SpyBus(), host="127.0.0.1", port=0)
            srv, port = await _serve_one(server)
            bus_a, bus_b = _SpyBus(), _SpyBus()
            client_a = NetworkClient(event_bus=bus_a, server_ip="127.0.0.1", server_port=port)
            client_b = NetworkClient(event_bus=bus_b, server_ip="127.0.0.1", server_port=port)
            client_a.reader, client_a.writer = await asyncio.open_connection("127.0.0.1", port)
            client_b.reader, client_b.writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.05)
            assert len(server._clients) == 2

            await server.send_event(Event(EventType.TrialCompleted, data=1))
            await client_a.receive()
            await client_b.receive()
            assert bus_a.events and bus_b.events  # both clients received it

            await client_a.stop()
            await client_b.stop()
            srv.close()
        asyncio.run(scenario())

    def test_unknown_event_type_is_dropped_not_crashing(self):
        async def scenario():
            from omnibci.infrastructure.ipc.network import _decode_event
            import json
            assert _decode_event(json.dumps({"type": 999999, "data": 1}).encode()) is None
            assert _decode_event(b"not json") is None
            assert _decode_event(b"[]") is None
            ev = _decode_event(json.dumps({"type": int(EventType.SessionEnded), "data": 2}).encode())
            assert ev is not None and ev.type == EventType.SessionEnded and ev.data == 2
        asyncio.run(scenario())


class TestClientLimit:
    def test_invalid_limit_is_rejected(self):
        with pytest.raises(ValueError, match="max_clients"):
            NetworkServer(max_clients=0)

    def test_connection_over_limit_is_closed_before_reading(self):
        class _Reader:
            async def readexactly(self, _):  # pragma: no cover - must not run
                raise AssertionError("rejected connection must not be read")

        class _Writer:
            def __init__(self):
                self.closed = False
                self.waited = False

            def get_extra_info(self, _):
                return ("127.0.0.1", 12345)

            def close(self):
                self.closed = True

            async def wait_closed(self):
                self.waited = True

        async def scenario():
            server = NetworkServer(max_clients=1)
            existing = object()
            server._clients.add(existing)
            writer = _Writer()
            await server.handle_client(_Reader(), writer)
            assert writer.closed is True and writer.waited is True
            assert server._clients == {existing}

        asyncio.run(scenario())


class TestReconnectBackoff:
    def test_immediate_disconnects_are_rate_limited(self, monkeypatch):
        client = NetworkClient(reconnect_initial=0.25, reconnect_max=1.0)
        attempts = 0
        delays = []

        async def connect():
            nonlocal attempts
            attempts += 1
            return True

        async def receive():
            if attempts == 3:
                client._should_run = False
            raise asyncio.IncompleteReadError(partial=b"", expected=1)

        async def close_writer():
            return None

        async def sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(client, "_connect", connect)
        monkeypatch.setattr(client, "receive", receive)
        monkeypatch.setattr(client, "_close_writer", close_writer)
        monkeypatch.setattr(asyncio, "sleep", sleep)
        monkeypatch.setattr(
            "omnibci.infrastructure.ipc.network.time.monotonic",
            lambda: 0.0,
        )

        asyncio.run(client.start())

        assert attempts == 3
        assert delays == [0.25, 0.5]


# ---------------------------------------------------------------------------
# 3. ClientMessageManagement task reference
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        await asyncio.Event().wait()  # block until cancelled

    async def stop(self) -> None:
        self.stopped = True


class TestClientMessageManagementTaskRef:
    def _make(self):
        from omnibci.infrastructure.stimulation.client import ClientMessageManagement
        m = ClientMessageManagement.__new__(ClientMessageManagement)
        m.event_bus = _SpyBus()
        m.client = _FakeClient()
        m._client_task = None
        return m

    def test_start_retains_task(self):
        async def scenario():
            m = self._make()
            await m.start()
            assert isinstance(m._client_task, asyncio.Task)
            await asyncio.sleep(0)
            assert not m._client_task.done()
            await m.stop()
        asyncio.run(scenario())

    def test_stop_cancels_and_clears_task(self):
        async def scenario():
            m = self._make()
            await m.start()
            await asyncio.sleep(0)
            task = m._client_task
            await m.stop()
            assert m.client.stopped is True
            assert task.cancelled() or task.done()
            assert m._client_task is None
        asyncio.run(scenario())
