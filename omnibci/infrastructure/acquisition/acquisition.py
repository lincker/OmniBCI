"""
Acquisition — 领域逻辑层。

包含：
  - AcquisitionCommand  采集命令枚举
  - AcquisitionState    采集状态枚举
  - Acquisition         领域服务：驱动 SignalSource 读取 EEG 数据

不包含任何 multiprocessing 代码；进程包装在 worker.py。
"""
import asyncio
import logging
import time
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from omnibci.application.events import AcquisitionSnapshot
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.domain.signal import SignalChunk
from omnibci.shared.events import Event, EventType

logger = logging.getLogger(__name__)


@runtime_checkable
class CommandQueue(Protocol):
    """采集进程消费的命令队列契约（mp.Queue 的最小可用子集）。"""

    def empty(self) -> bool: ...

    def get(self) -> "AcquisitionCommand": ...


@runtime_checkable
class DataSink(Protocol):
    """采集数据下游汇聚点契约（共享 DataManager 的最小可用子集）。"""

    def put_data(self, chunk: SignalChunk) -> None: ...


class AcquisitionCommand(Enum):
    start_acquisition = 'start acquisition'
    stop_acquisition  = 'stop acquisition'
    get_metadata      = 'get metadata'
    connect           = 'connect'
    start_stream      = 'start stream'
    stop_stream       = 'stop stream'
    disconnect        = 'disconnect'


class AcquisitionState(Enum):
    stop    = 'stop'
    running = 'running'
    paused  = 'paused'


class Acquisition:
    """
    Domain service: drives a SignalSource to read EEG data and pushes
    SignalChunk objects into the shared DataManager buffer.
    """

    def __init__(
        self,
        data_source: SignalSource,
        command_queue: CommandQueue,
        data_manager: DataSink,
        event_bus=None,
    ) -> None:
        self.data_source = data_source
        self.command_queue = command_queue
        # 注意区分：本地 AcquisitionState 枚举是 worker 内部相位（stop/running），
        # EventType.AcquisitionState 是发往 API 投影的 wire 事件。
        self.status = AcquisitionState.stop
        self.fetch_task: Optional[asyncio.Task] = None
        self.data_manager = data_manager
        self.event_bus = event_bus   # None = 无投影（单测）；只用于 put_event
        self.should_exit = False

    def run(self) -> None:
        asyncio.run(self.serve())

    async def serve(self) -> None:
        await self.startup()
        await self.main_loop()
        await self.shutdown()

    async def startup(self) -> None:
        logger.info('Acquisition started')

    async def shutdown(self) -> None:
        logger.info('Acquisition shutting down')
        if self.data_source.is_connected:
            await self.data_source.disconnect()
        self._emit_state()

    async def main_loop(self) -> None:
        while not self.should_exit:
            if not self.command_queue.empty():
                await self.execute(self.command_queue.get())
            await asyncio.sleep(0.01)

    async def execute(self, command: AcquisitionCommand) -> None:
        """把命令分派到对应处理器；未知命令仅告警，不改变状态。"""
        logger.debug('Acquisition command: %s', command)
        handler = {
            AcquisitionCommand.connect:          self._on_connect,
            AcquisitionCommand.disconnect:       self._on_disconnect,
            AcquisitionCommand.start_stream:     self._on_start_stream,
            AcquisitionCommand.stop_stream:      self._on_stop_stream,
            AcquisitionCommand.stop_acquisition: self._on_stop_acquisition,
            AcquisitionCommand.get_metadata:     self._on_get_metadata,
        }.get(command)
        if handler is None:
            logger.warning('Unknown acquisition command: %s', command)
            return
        await handler()

    async def _on_connect(self) -> None:
        await self.data_source.connect()
        self.status = AcquisitionState.running
        self._emit_state()

    async def _on_disconnect(self) -> None:
        await self.data_source.disconnect()
        self.status = AcquisitionState.stop
        self._emit_state()

    async def _on_start_stream(self) -> None:
        # start_stream is only honoured once connected (status == running);
        # otherwise it is dropped — but the truth (not streaming) is still
        # pushed to the projection so the API cannot report a phantom stream.
        if self.status != AcquisitionState.running:
            logger.warning('start_stream ignored: not running (status=%s)', self.status)
            self._emit_state()
            return
        await self.data_source.start_stream()
        self.fetch_task = asyncio.create_task(self.fetch())
        logger.info('Fetch task started')
        self._emit_state()

    async def _on_stop_stream(self) -> None:
        await self.data_source.stop_stream()
        if self.fetch_task:
            self.fetch_task.cancel()
        logger.info('Fetch task stopped')
        self._emit_state()

    async def _on_stop_acquisition(self) -> None:
        self.should_exit = True

    async def _on_get_metadata(self) -> None:
        info = await self.data_source.get_metadata()
        logger.info('Device metadata: %s', info)

    async def fetch(self) -> None:
        packets_counter = 0
        try:
            while True:
                raw_data = await self.data_source.read_chunk()
                if raw_data is None:
                    # 数据源正常播完（如文件回放 EOF）：干净收流并把真相
                    # 推回 API 投影，否则前端会永远显示"读取中"。
                    logger.info('Data source end of stream after %d chunks; fetch task finished',
                                packets_counter)
                    await self._finish_stream()
                    break
                chunk = SignalChunk(
                    timestamp=time.time_ns(),
                    raw_data=raw_data,
                    sequence_id=packets_counter,
                )
                chunk = self.preprocess(chunk)
                self.data_manager.put_data(chunk)
                packets_counter += 1
        except asyncio.CancelledError:
            # 取消来自 _on_stop_stream，那里已 stop_stream 并发射快照。
            logger.info('Fetch task cancelled')
        except Exception as exc:
            logger.error('Error in fetch task: %s', exc)
            await self._finish_stream()

    def preprocess(self, data: SignalChunk) -> SignalChunk:
        # TODO: channel mapping per selected algorithm
        return data

    async def _finish_stream(self) -> None:
        """Close the stream from inside fetch() (EOF / fatal error) and
        publish the resulting state."""
        try:
            await self.data_source.stop_stream()
        except Exception as exc:
            logger.error('stop_stream during fetch teardown failed: %s', exc)
        self._emit_state()

    def _emit_state(self) -> None:
        """Publish the device's authoritative state to the API-side projection.

        The snapshot reads the SignalSource port properties directly — the
        single source of truth — rather than tracking booleans by hand.
        """
        if self.event_bus is None:
            return
        self.event_bus.put_event(Event(
            EventType.AcquisitionState,
            AcquisitionSnapshot(
                connected=self.data_source.is_connected,
                streaming=self.data_source.is_streaming,
            ),
        ))
