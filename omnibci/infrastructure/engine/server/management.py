"""
ServerMessageManagement — 管理 NetworkServer 生命周期。

职责：向连接的刺激端（或其他外部客户端）推送结果事件。

删除内容：
  - CommandServer（aiohttp HTTP server，端口 8080）
    原因：所有外部命令已通过 FastAPI /api/v1/ 路由处理，
    两套命令入口并存逻辑重复，路由语义不一致（StartReceive vs StartStream）。
    现在 FastAPI 是唯一命令入口。
"""
import asyncio
import logging
from typing import Optional

from omnibci.shared.events import EventType
from omnibci.infrastructure.ipc.network import NetworkServer

logger = logging.getLogger(__name__)


class ServerMessageManagement:
    def __init__(self, event_bus, host: str, port: int) -> None:
        self.event_bus = event_bus
        self.server = NetworkServer(
            event_bus=event_bus,
            host=host,
            port=port,
        )
        # Strong reference to the background server task.  Without it the event
        # loop only keeps a weak reference and the task may be garbage-collected
        # mid-run ("Task was destroyed but it is pending"), silently stopping the
        # result-push server.
        self._server_task: Optional[asyncio.Task] = None
        self._register_events()

    def start(self) -> None:
        self._server_task = asyncio.create_task(self.server.start())

    async def stop(self) -> None:
        await self.server.stop()
        # Cancel the lingering server task and await its completion so shutdown
        # is clean and no pending-task warning is emitted.
        if self._server_task is not None and not self._server_task.done():
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("ServerMessageManagement: error awaiting server task: %s", exc)
        self._server_task = None

    def _register_events(self) -> None:
        """Register domain events that should be pushed to connected clients."""
        self.event_bus.register_event_listener(EventType.TrialCompleted, self.server.send_event)
        self.event_bus.register_event_listener(EventType.StartStimTrial, self.server.send_event)
        self.event_bus.register_event_listener(EventType.StopStimulation, self.server.send_event)
