"""Acquisition command confirmation across the real EventBus/queue wiring."""

from __future__ import annotations

import asyncio
import multiprocessing as mp

from omnibci.application.events import AcquisitionSnapshot, CommandRequest
from omnibci.application.ports.engine_port import EnginePort
from omnibci.application.services.command_result_service import CommandResultBroker
from omnibci.infrastructure.acquisition.acquisition import (
    Acquisition,
    AcquisitionCommand,
)
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.shared.events import Event, EventType


class _BusEngine(EnginePort):
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    @property
    def is_initialized(self) -> bool:
        return True

    @property
    def is_engine_ready(self) -> bool:
        return True

    def dispatch(self, event: Event) -> None:
        self._bus.put_event(event)


class _Source:
    def __init__(self) -> None:
        self._is_connected = False
        self._is_streaming = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    async def connect(self) -> bool:
        self._is_connected = True
        return True

    async def disconnect(self) -> bool:
        self._is_connected = False
        return True

    async def start_stream(self) -> bool:
        self._is_streaming = True
        return True

    async def stop_stream(self) -> bool:
        self._is_streaming = False
        return True

    async def read_chunk(self):  # pragma: no cover - connect test never reads
        return None

    async def get_metadata(self):  # pragma: no cover - unused
        return None


class _DataManager:
    def put_data(self, _chunk) -> None:  # pragma: no cover - unused
        pass


def test_connect_round_trips_through_event_bus_worker_and_broker() -> None:
    async def scenario() -> None:
        event_queue = mp.Queue()
        command_queue = mp.Queue()
        bus = EventBus(event_queue)
        source = _Source()
        acquisition = Acquisition(
            data_source=source,
            command_queue=command_queue,
            data_manager=_DataManager(),
            event_bus=bus,
        )
        broker = CommandResultBroker(_BusEngine(bus), timeout_s=1.0)
        observed_states: list[AcquisitionSnapshot] = []

        bus.register_event_listener(
            EventType.ConnectDataProvider,
            NeuroEngine._make_command_router(
                command_queue,
                AcquisitionCommand.connect,
                carry_data=True,
                event_bus=bus,
            ),
        )
        bus.register_event_listener(EventType.AcquisitionState,
                                    lambda event: observed_states.append(event.data))
        bus.register_event_listener(EventType.CommandResult, broker.on_result)

        await bus.start()
        try:
            async def execute_one_command() -> None:
                item = await asyncio.to_thread(command_queue.get, True, 1.0)
                command, request = item
                assert command is AcquisitionCommand.connect
                assert isinstance(request, CommandRequest)
                await acquisition.execute(item)

            worker = asyncio.create_task(execute_one_command())
            result = await asyncio.to_thread(
                broker.request,
                EventType.ConnectDataProvider,
            )
            await worker

            assert result.ok is True
            assert result.command == "ConnectDataProvider"
            assert source.is_connected is True
            assert observed_states == [
                AcquisitionSnapshot(connected=True, streaming=False)
            ]
            assert broker.pending_count == 0
        finally:
            await bus.stop()
            for queue in (command_queue, event_queue):
                queue.close()
                queue.join_thread()

    asyncio.run(scenario())
