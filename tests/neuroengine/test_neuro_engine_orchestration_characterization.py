"""
Characterization safety net — NeuroEngine orchestration.

Complements tests/neuroengine/test_neuro_engine.py.  That file asserts routing
by calling the *private* handler methods (``_connect_data_provider`` …) directly.
The upcoming P1 refactor will collapse those methods into a data-driven routing
map, so method-name-coupled tests would break by construction.

This file instead pins the **observable contract through the public seam**:
  register listeners on a fake EventBus, then invoke whatever handler the bus
  received for each EventType, and assert the correct command lands on the
  correct command queue.  That contract must survive the routing-map refactor.

Also pins the full ``serve()`` lifecycle (start → loop exits on should_stop →
stop), which the existing file does not cover.

Coroutines are driven with ``asyncio.run`` so the suite needs no pytest-asyncio.
"""
from __future__ import annotations

import asyncio

import pytest

from omnibci.shared.events import EventType, Event
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessorCommand


# ---------------------------------------------------------------------------
# Fakes (no multiprocessing, no network, no real bus)
# ---------------------------------------------------------------------------

class FakeBus:
    def __init__(self) -> None:
        self.listeners: dict = {}
        self.started = False
        self.stopped = False

    def register_event_listener(self, event_type, handler) -> None:
        self.listeners.setdefault(event_type, []).append(handler)

    async def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class FakeQueue:
    def __init__(self) -> None:
        self.items: list = []
        self.closed = False

    def put(self, item) -> None:
        self.items.append(item)

    def close(self) -> None:
        self.closed = True

    def join_thread(self) -> None:
        pass


class FakeProcess:
    def __init__(self, alive_after_join: bool = False) -> None:
        self.started = False
        self.joined = False
        self.terminated = False
        self._alive = alive_after_join

    def start(self) -> None:
        self.started = True

    def join(self, timeout=None) -> None:
        self.joined = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminated = True


class FakeServerMgmt:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _make_engine() -> NeuroEngine:
    return NeuroEngine(
        event_bus=FakeBus(),
        data_manager=object(),
        acquisition_worker=FakeProcess(),
        acquisition_command_queue=FakeQueue(),
        signal_processing_worker=FakeProcess(),
        processor_command_queue=FakeQueue(),
        peer_gateway=FakeServerMgmt(),
    )


# Observable routing contract: EventType → (which queue, which command).
_ACQ_ROUTES = {
    EventType.ConnectDataProvider: AcquisitionCommand.connect,
    EventType.DisconnectDataProvider: AcquisitionCommand.disconnect,
    EventType.StartStream: AcquisitionCommand.start_stream,
    EventType.StopStream: AcquisitionCommand.stop_stream,
}


# ---------------------------------------------------------------------------
# Listener registration is the seam P1 will change — pin it via the bus.
# ---------------------------------------------------------------------------

class TestListenerRegistration:
    def test_registers_expected_event_types(self):
        eng = _make_engine()
        eng._register_event_listeners()
        registered = set(eng.event_bus.listeners.keys())
        assert registered == {
            EventType.ConnectDataProvider,
            EventType.DisconnectDataProvider,
            EventType.StartStream,
            EventType.StopStream,
            EventType.StartSession,
            EventType.EndSession,
            EventType.CloseAllNeuroEngineProcessSystem,
            EventType.StimulationAck,
        }

    def test_each_event_has_exactly_one_handler(self):
        eng = _make_engine()
        eng._register_event_listeners()
        assert all(len(hs) == 1 for hs in eng.event_bus.listeners.values())


# ---------------------------------------------------------------------------
# Routing contract: invoke the *registered* handler, assert command on queue.
# This is independent of the private method names the routing uses internally.
# ---------------------------------------------------------------------------

class TestRoutingThroughRegisteredHandler:
    @pytest.mark.parametrize("event_type,command", list(_ACQ_ROUTES.items()))
    def test_acquisition_routes(self, event_type, command):
        eng = _make_engine()
        eng._register_event_listeners()
        handler = eng.event_bus.listeners[event_type][0]
        handler(Event(event_type))  # bus invokes handlers with an Event
        assert eng.acquisition_command_queue.items == [command]
        assert eng.processor_command_queue.items == []

    def test_close_all_sets_should_stop_without_queueing(self):
        eng = _make_engine()
        eng._register_event_listeners()
        handler = eng.event_bus.listeners[EventType.CloseAllNeuroEngineProcessSystem][0]
        assert eng.should_stop is False
        handler(Event(EventType.CloseAllNeuroEngineProcessSystem))
        assert eng.should_stop is True
        assert eng.acquisition_command_queue.items == []
        assert eng.processor_command_queue.items == []


# ---------------------------------------------------------------------------
# Lifecycle: _start, _stop, and full serve()
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_start_brings_up_all_subsystems(self):
        eng = _make_engine()
        asyncio.run(eng._start())
        assert eng.acquisition_worker.started is True
        assert eng.signal_processing_worker.started is True
        assert eng.event_bus.started is True
        assert eng.peer_gateway.started is True
        assert len(eng.event_bus.listeners) == 8

    def test_stop_tears_down_all_subsystems(self):
        eng = _make_engine()
        asyncio.run(eng._stop())
        # stop commands sent
        assert AcquisitionCommand.stop_acquisition in eng.acquisition_command_queue.items
        assert eng.processor_command_queue.items == [SignalProcessorCommand.should_stop]
        # bus + server stopped
        assert eng.event_bus.stopped is True
        assert eng.peer_gateway.stopped is True
        # processes joined, queues closed
        assert eng.acquisition_worker.joined and eng.signal_processing_worker.joined
        assert eng.acquisition_command_queue.closed and eng.processor_command_queue.closed

    def test_stop_terminates_hung_process(self):
        eng = NeuroEngine(
            event_bus=FakeBus(),
            data_manager=object(),
            acquisition_worker=FakeProcess(alive_after_join=True),  # hangs
            acquisition_command_queue=FakeQueue(),
            signal_processing_worker=FakeProcess(),
            processor_command_queue=FakeQueue(),
            peer_gateway=FakeServerMgmt(),
        )
        asyncio.run(eng._stop())
        assert eng.acquisition_worker.terminated is True

    def test_serve_runs_start_then_stop_when_should_stop_preset(self):
        eng = _make_engine()
        eng.should_stop = True  # _main_loop exits immediately
        asyncio.run(eng.serve())
        # both bring-up and tear-down ran
        assert eng.peer_gateway.started is True
        assert eng.peer_gateway.stopped is True
        assert eng.event_bus.started is True
        assert eng.event_bus.stopped is True


# ---------------------------------------------------------------------------
# P2-7: 主循环事件驱动停机（sleep(0.05) 轮询已移除）
# ---------------------------------------------------------------------------

class TestEventDrivenShutdown:
    def test_cross_thread_stop_wakes_main_loop_promptly(self):
        """从另一线程置 should_stop：serve 应即刻退出，而非等轮询周期。"""
        import threading
        import time

        eng = _make_engine()
        t0 = {}

        def stopper():
            time.sleep(0.05)          # 让 serve 进入等待
            t0["set_at"] = time.perf_counter()
            eng.should_stop = True     # API 线程视角的置停

        threading.Thread(target=stopper).start()
        asyncio.run(eng.serve())
        elapsed = time.perf_counter() - t0["set_at"]
        assert elapsed < 0.5           # 事件唤醒；旧实现上界为一个轮询周期
        assert eng.peer_gateway.stopped and eng.event_bus.stopped

    def test_stop_flag_set_before_serve_exits_immediately(self):
        """serve 前预置 should_stop（既有 characterization 场景）仍然成立。"""
        eng = _make_engine()
        eng.should_stop = True
        asyncio.run(eng.serve())
        assert eng.event_bus.started and eng.event_bus.stopped
