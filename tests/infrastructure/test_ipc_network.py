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

Round-trips use a real asyncio server on 127.0.0.1 with an ephemeral port —
no external services, no settings, no multiprocessing.
"""
from __future__ import annotations

import asyncio

import pytest

from omnibci.infrastructure.ipc.contracts import (
    InvalidEventBody,
    PROTOCOL_VERSION,
    encode_event,
)
from omnibci.infrastructure.ipc.protocol import Frame, MAX_FRAME_BODY_BYTES
from omnibci.infrastructure.ipc.network import (
    NetworkClient,
    NetworkServer,
    StreamCleanupError,
)
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

    def test_frame_public_fields_are_read_only(self):
        frame = Frame("abc")
        with pytest.raises(AttributeError):
            frame.body = "longer"
        with pytest.raises(AttributeError):
            frame.header.body_size = 1

        encoded = frame.to_bytes()
        header = Frame.Header.from_bytes(encoded[: Frame.Header.HEADER_SIZE])
        assert header.body_size == len(encoded[Frame.Header.HEADER_SIZE:]) == 3


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
    await server.start()
    srv = server.server
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
            await server.stop()
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
            await server.stop()
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
            await server.stop()
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
            await server.stop()
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

    def test_unconfirmed_closing_writer_still_occupies_control_slot(self):
        class _Reader:
            async def readexactly(self, _):  # pragma: no cover - must not run
                raise AssertionError("replacement must not be read")

        class _Writer:
            def __init__(self):
                self.closed = False

            def get_extra_info(self, _):
                return ("127.0.0.1", 12346)

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        async def scenario():
            server = NetworkServer(max_clients=1)
            old_writer = object()
            server._closing_writers.add(old_writer)
            replacement = _Writer()

            await server.handle_client(_Reader(), replacement)

            assert replacement.closed is True
            assert server._clients == set()
            assert server._closing_writers == {old_writer}

        asyncio.run(scenario())


class TestServerLifecycle:
    def test_start_is_bind_ready_and_start_stop_are_idempotent(self):
        async def scenario():
            server = NetworkServer(host="127.0.0.1", port=0)

            await server.start()
            bound_server = server.server
            assert bound_server is not None
            assert bound_server.is_serving()
            assert bound_server.sockets[0].getsockname()[1] > 0
            assert server.status == "RUNNING"

            await server.start()
            assert server.server is bound_server

            await server.stop()
            await server.stop()
            assert server.server is None
            assert server.status == "STOP"

        asyncio.run(scenario())

    def test_bind_failure_is_reported_by_start(self):
        async def scenario():
            blocker = await asyncio.start_server(
                lambda _reader, _writer: None,
                host="127.0.0.1",
                port=0,
            )
            port = blocker.sockets[0].getsockname()[1]
            server = NetworkServer(host="127.0.0.1", port=port)
            try:
                with pytest.raises(OSError):
                    await server.start()
                assert server.status == "FAILED"
                assert server.server is None
            finally:
                blocker.close()
                await blocker.wait_closed()

        asyncio.run(scenario())

    @pytest.mark.parametrize("resource", ["writer", "task"])
    def test_start_rejects_residual_client_resources(self, resource):
        async def scenario():
            server = NetworkServer(host="127.0.0.1", port=0)
            if resource == "writer":
                server._closing_writers.add(object())
            else:
                server._client_tasks.add(object())

            with pytest.raises(
                StreamCleanupError, match="residual client resources"
            ):
                await server.start()
            assert server.status == "STOP_FAILED"
            assert server.server is None

        asyncio.run(scenario())

    def test_stop_closes_clients_and_waits_for_handlers(self):
        async def scenario():
            server = NetworkServer(host="127.0.0.1", port=0)
            _, port = await _serve_one(server)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            await asyncio.sleep(0.01)
            assert server._clients
            assert server._client_tasks

            await server.stop()

            assert await asyncio.wait_for(reader.read(1), timeout=0.2) == b""
            assert server._clients == set()
            assert server._client_tasks == set()
            writer.close()
            await writer.wait_closed()

        asyncio.run(scenario())

    def test_listener_timeout_retains_owner_and_stop_can_be_retried(self):
        class _Listener:
            def __init__(self):
                self.release = asyncio.Event()
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

            async def wait_closed(self):
                await self.release.wait()

        async def scenario():
            server = NetworkServer(write_timeout=0.01)
            listener = _Listener()
            server.server = listener

            with pytest.raises(
                StreamCleanupError, match="listener close was not confirmed"
            ):
                await server.stop()
            assert server.status == "STOP_FAILED"
            assert server.server is listener

            listener.release.set()
            await server.stop()
            assert server.status == "STOP"
            assert server.server is None
            assert listener.close_calls == 2

        asyncio.run(scenario())

    def test_writer_timeout_retains_owner_and_stop_can_be_retried(self):
        class _Writer:
            def __init__(self):
                self.release = asyncio.Event()

            def close(self):
                return None

            async def wait_closed(self):
                await self.release.wait()

        async def scenario():
            server = NetworkServer(write_timeout=0.01)
            writer = _Writer()
            server._clients.add(writer)

            with pytest.raises(
                StreamCleanupError, match="writer close.*not confirmed"
            ):
                await server.stop()
            assert server.status == "STOP_FAILED"
            assert writer in server._closing_writers

            writer.release.set()
            await server.stop()
            assert server.status == "STOP"
            assert server._closing_writers == set()

        asyncio.run(scenario())

    def test_stop_waits_for_in_flight_send_and_rejects_late_send(self):
        class _Writer:
            def __init__(self):
                self.drain_entered = asyncio.Event()
                self.drain_release = asyncio.Event()
                self.closed = False
                self.write_calls = 0

            def write(self, _frame):
                self.write_calls += 1

            async def drain(self):
                self.drain_entered.set()
                await self.drain_release.wait()

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        async def scenario():
            server = NetworkServer(write_timeout=1.0)
            writer = _Writer()
            server._clients.add(writer)

            send_task = asyncio.create_task(server.send("before-stop"))
            await writer.drain_entered.wait()
            stop_task = asyncio.create_task(server.stop())
            await asyncio.sleep(0)

            assert stop_task.done() is False
            assert writer.closed is False
            writer.drain_release.set()

            assert await send_task is True
            await stop_task
            assert writer.closed is True
            assert server.status == "STOP"
            assert await server.send("after-stop") is False
            assert writer.write_calls == 1

        asyncio.run(scenario())


class TestSafeFrameRead:
    @pytest.mark.parametrize(
        "invalid_body",
        [
            "not json",
            "[]",
            '{"type":999999,"data":1}',
        ],
        ids=["malformed-json", "non-object", "unknown-event"],
    )
    def test_invalid_complete_event_disconnects_without_liveness(
        self, invalid_body
    ):
        async def scenario():
            bus = _SpyBus()
            server = NetworkServer(event_bus=bus, host="127.0.0.1", port=0)
            liveness_calls = 0

            def on_liveness():
                nonlocal liveness_calls
                liveness_calls += 1

            server.set_liveness_handler(on_liveness)
            _, port = await _serve_one(server)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                Frame(invalid_body, version=PROTOCOL_VERSION).to_bytes()
                + encode_event(Event(EventType.StartStimTrial, data=4))
            )
            await writer.drain()

            # The invalid frame is complete and aligned, but keeping this peer
            # would let unusable traffic monopolise max_clients=1 indefinitely.
            assert await asyncio.wait_for(reader.read(1), timeout=0.2) == b""
            await asyncio.sleep(0)
            assert liveness_calls == 0
            assert bus.events == []
            assert server._clients == set()

            writer.close()
            await writer.wait_closed()
            await server.stop()

        asyncio.run(scenario())

    def test_event_liveness_requires_route_acceptance_keepalive_does_not(self):
        async def scenario():
            server = NetworkServer(host="127.0.0.1", port=0)
            seen = []
            liveness_calls = 0

            def on_event(event):
                seen.append(event.data)
                return event.data == "accepted"

            def on_liveness():
                nonlocal liveness_calls
                liveness_calls += 1

            server.set_inbound_handler(on_event)
            server.set_liveness_handler(on_liveness)
            _, port = await _serve_one(server)
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                encode_event(Event(EventType.ActuationState, "rejected"))
                + Frame("", version=PROTOCOL_VERSION).to_bytes()
                + encode_event(Event(EventType.ActuationState, "accepted"))
            )
            await writer.drain()
            await asyncio.sleep(0.05)

            assert seen == ["rejected", "accepted"]
            assert liveness_calls == 2  # keepalive + accepted Event

            writer.close()
            await writer.wait_closed()
            await server.stop()

        asyncio.run(scenario())

    def test_client_uses_same_acceptance_and_keepalive_liveness_policy(self):
        async def scenario():
            reader = asyncio.StreamReader()
            client = NetworkClient(event_bus=None)
            client.reader = reader
            seen = []
            liveness_calls = 0

            def on_event(event):
                seen.append(event.data)
                return event.data == "accepted"

            def on_liveness():
                nonlocal liveness_calls
                liveness_calls += 1

            client.set_inbound_handler(on_event)
            client.set_liveness_handler(on_liveness)

            reader.feed_data(encode_event(Event(EventType.ActuationState, "rejected")))
            await client.receive()
            assert liveness_calls == 0

            reader.feed_data(Frame("", version=PROTOCOL_VERSION).to_bytes())
            await client.receive()
            assert liveness_calls == 1

            reader.feed_data(encode_event(Event(EventType.ActuationState, "accepted")))
            await client.receive()
            assert seen == ["rejected", "accepted"]
            assert liveness_calls == 2

        asyncio.run(scenario())

    def test_client_rejects_complete_invalid_event_body(self):
        async def scenario():
            reader = asyncio.StreamReader()
            client = NetworkClient(event_bus=_SpyBus())
            client.reader = reader
            reader.feed_data(Frame("not json").to_bytes())

            with pytest.raises(InvalidEventBody):
                await client.receive()

        asyncio.run(scenario())

    def test_legacy_event_bus_path_counts_successful_injection_as_accepted(self):
        async def scenario():
            reader = asyncio.StreamReader()
            bus = _SpyBus()
            client = NetworkClient(event_bus=bus)
            client.reader = reader
            liveness_calls = 0

            def on_liveness():
                nonlocal liveness_calls
                liveness_calls += 1

            client.set_liveness_handler(on_liveness)
            reader.feed_data(encode_event(Event(EventType.TrialCompleted, 7)))
            await client.receive()

            assert [event.data for event in bus.events] == [7]
            assert liveness_calls == 1

        asyncio.run(scenario())

    @pytest.mark.parametrize(
        "header",
        [
            (PROTOCOL_VERSION + 99).to_bytes(4, "big") + (1).to_bytes(4, "big"),
            (PROTOCOL_VERSION + 99).to_bytes(4, "big") + (0).to_bytes(4, "big"),
            PROTOCOL_VERSION.to_bytes(4, "big")
            + (MAX_FRAME_BODY_BYTES + 1).to_bytes(4, "big"),
        ],
        ids=[
            "unsupported-version",
            "unsupported-empty-keepalive",
            "oversized-body",
        ],
    )
    def test_invalid_header_is_rejected_before_waiting_for_body(self, header):
        async def scenario():
            server = NetworkServer(host="127.0.0.1", port=0)
            liveness_calls = 0

            def on_liveness():
                nonlocal liveness_calls
                liveness_calls += 1

            server.set_liveness_handler(on_liveness)
            _, port = await _serve_one(server)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(header)  # deliberately do not send the advertised body
            await writer.drain()

            assert await asyncio.wait_for(reader.read(1), timeout=0.2) == b""
            assert liveness_calls == 0

            writer.close()
            await writer.wait_closed()
            await server.stop()

        asyncio.run(scenario())

    def test_optional_idle_timeout_closes_silent_peer(self):
        async def scenario():
            server = NetworkServer(
                host="127.0.0.1",
                port=0,
                idle_timeout=0.02,
            )
            _, port = await _serve_one(server)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            assert await asyncio.wait_for(reader.read(1), timeout=0.2) == b""
            await asyncio.sleep(0)
            assert not server._clients

            writer.close()
            await writer.wait_closed()
            await server.stop()

        asyncio.run(scenario())


class TestSendSemantics:
    def test_server_send_returns_delivery_status_and_serializes_frames(self):
        async def scenario():
            server = NetworkServer(event_bus=_SpyBus(), host="127.0.0.1", port=0)
            _, port = await _serve_one(server)
            assert await server.send_event(Event(EventType.TrialCompleted, 0)) is False

            bus = _SpyBus()
            client = NetworkClient(event_bus=bus)
            client.reader, client.writer = await asyncio.open_connection(
                "127.0.0.1", port
            )
            await asyncio.sleep(0.01)

            delivered = await asyncio.gather(
                server.send_event(Event(EventType.TrialCompleted, 1)),
                server.send_event(Event(EventType.TrialCompleted, 2)),
            )
            assert delivered == [True, True]
            await client.receive()
            await client.receive()
            assert {event.data for event in bus.events} == {1, 2}

            await client.stop()
            await server.stop()

        asyncio.run(scenario())

    def test_server_fanout_drain_is_concurrent(self):
        class _BarrierWriter:
            def __init__(self, barrier, entered):
                self.barrier = barrier
                self.entered = entered
                self.frames = []
                self.closed = False

            def write(self, frame):
                self.frames.append(frame)

            async def drain(self):
                self.entered.append(self)
                if len(self.entered) == 2:
                    self.barrier.set()
                await self.barrier.wait()

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        async def scenario():
            barrier = asyncio.Event()
            entered = []
            writers = {
                _BarrierWriter(barrier, entered),
                _BarrierWriter(barrier, entered),
            }
            server = NetworkServer(write_timeout=0.1)
            server._clients.update(writers)

            assert await server.send("message") is True
            assert len(entered) == 2
            assert all(writer.frames for writer in writers)

        asyncio.run(scenario())

    def test_client_send_returns_false_and_detaches_failed_writer(self):
        class _Writer:
            def __init__(self, failure=None):
                self.failure = failure
                self.closed = False
                self.waited = False
                self.frames = []

            def write(self, frame):
                self.frames.append(frame)

            async def drain(self):
                if self.failure is not None:
                    raise self.failure

            def close(self):
                self.closed = True

            async def wait_closed(self):
                self.waited = True

        async def scenario():
            client = NetworkClient()
            assert await client.send("message") is False

            good = _Writer()
            client.writer = good
            assert await client.send("message") is True
            assert good.frames

            failed = _Writer(ValueError("closed"))
            client.writer = failed
            client.reader = object()
            assert await client.send("message") is False
            assert client.writer is None and client.reader is None
            assert failed.closed and failed.waited

        asyncio.run(scenario())

    def test_client_write_and_close_waits_are_bounded(self):
        class _StalledWriter:
            def __init__(self):
                self.closed = False
                self.close_release = asyncio.Event()

            def write(self, _frame):
                return None

            async def drain(self):
                await asyncio.Event().wait()

            def close(self):
                self.closed = True

            async def wait_closed(self):
                await self.close_release.wait()

        async def scenario():
            client = NetworkClient(write_timeout=0.01)
            writer = _StalledWriter()
            client.writer = writer
            client.reader = object()

            assert await asyncio.wait_for(client.send("message"), timeout=0.1) is False
            # A bounded close timeout is not proof of release.  Keep the handle
            # and an explicit failed-cleanup state so a later stop can retry.
            assert client.writer is writer and client.reader is not None
            assert writer in client._closing_writers
            assert client.status == "STOP_FAILED"
            assert writer.closed is True

            writer.close_release.set()
            await client.stop()
            assert client.writer is None and client.reader is None
            assert client._closing_writers == set()
            assert client.status == "STOP"

        asyncio.run(scenario())

    def test_connection_reset_during_wait_closed_is_confirmed_dead(self):
        class _ResetWriter:
            def close(self):
                return None

            async def wait_closed(self):
                raise ConnectionResetError("peer reset")

        async def scenario():
            client = NetworkClient()
            writer = _ResetWriter()
            client.writer = writer
            client.reader = object()

            await client.stop()

            assert client.status == "STOP"
            assert client.writer is None and client.reader is None
            assert client._closing_writers == set()

        asyncio.run(scenario())

    @pytest.mark.parametrize("value", [0, -1, None, True, float("nan"), float("inf")])
    def test_write_timeout_must_be_positive(self, value):
        with pytest.raises(ValueError, match="write_timeout"):
            NetworkClient(write_timeout=value)


class TestClientLifecycle:
    def test_stop_waits_for_in_flight_send_and_rejects_late_send(self):
        class _Writer:
            def __init__(self):
                self.drain_entered = asyncio.Event()
                self.drain_release = asyncio.Event()
                self.closed = False
                self.write_calls = 0

            def write(self, _frame):
                self.write_calls += 1

            async def drain(self):
                self.drain_entered.set()
                await self.drain_release.wait()

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        async def scenario():
            client = NetworkClient(write_timeout=1.0)
            writer = _Writer()
            client.writer = writer
            client.reader = object()
            client.status = "CONNECTED"

            send_task = asyncio.create_task(client.send("before-stop"))
            await writer.drain_entered.wait()
            stop_task = asyncio.create_task(client.stop())
            await asyncio.sleep(0)

            assert stop_task.done() is False
            assert writer.closed is False
            writer.drain_release.set()

            assert await send_task is True
            await stop_task
            assert writer.closed is True
            assert client.status == "STOP"
            assert await client.send("after-stop") is False
            assert writer.write_calls == 1

        asyncio.run(scenario())

    def test_supervisor_cleanup_waits_for_in_flight_send(self, monkeypatch):
        class _Writer:
            def __init__(self):
                self.drain_entered = asyncio.Event()
                self.drain_release = asyncio.Event()
                self.closed = False

            def write(self, _frame):
                return None

            async def drain(self):
                self.drain_entered.set()
                await self.drain_release.wait()

            def close(self):
                self.closed = True

            async def wait_closed(self):
                return None

        async def scenario():
            client = NetworkClient(write_timeout=1.0)
            writer = _Writer()
            receive_forever = asyncio.Event()

            async def connect():
                client.reader = object()
                client.writer = writer
                client.status = "CONNECTED"
                return True

            async def receive():
                await receive_forever.wait()

            monkeypatch.setattr(client, "_connect", connect)
            monkeypatch.setattr(client, "receive", receive)
            supervisor = asyncio.create_task(client.start())
            while client.status != "CONNECTED":
                await asyncio.sleep(0)

            send_task = asyncio.create_task(client.send("before-stop"))
            await writer.drain_entered.wait()
            stop_task = asyncio.create_task(client.stop())
            await asyncio.sleep(0)

            assert supervisor.done() is False
            assert stop_task.done() is False
            assert writer.closed is False

            writer.drain_release.set()
            assert await send_task is True
            await stop_task

            assert supervisor.done()
            assert writer.closed is True
            assert client.status == "STOP"

        asyncio.run(scenario())

    def test_connection_completing_after_stop_is_closed_not_installed(self, monkeypatch):
        class _Writer:
            def __init__(self):
                self.closed = False
                self.waited = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                self.waited = True

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()
            writer = _Writer()

            async def open_connection(*_args, **_kwargs):
                started.set()
                await release.wait()
                return object(), writer

            monkeypatch.setattr(asyncio, "open_connection", open_connection)
            client = NetworkClient(connect_timeout=1.0)
            client._should_run = True
            connect_task = asyncio.create_task(client._connect())
            await started.wait()

            await client.stop()
            release.set()

            assert await connect_task is False
            assert client.status == "STOP"
            assert client.writer is None and client.reader is None
            assert writer.closed and writer.waited

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
