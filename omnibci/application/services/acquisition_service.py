"""
AcquisitionAppService — Application Service.

Translates acquisition commands/queries into EventBus events that the
NeuroEngine already understands.  Keeps acquisition routing logic out of
the routers.
"""

import logging
from typing import Optional

from omnibci.shared.events import EventType, Event
from omnibci.application.commands.acquisition_commands import (
    ConnectDataProviderCommand,
    DisconnectDataProviderCommand,
    StartStreamCommand,
    StopStreamCommand,
)
from omnibci.application.queries.acquisition_queries import (
    GetAcquisitionStatusQuery,
    AcquisitionStatusResult,
)
from omnibci.application.ports.engine_port import EnginePort

logger = logging.getLogger(__name__)


class AcquisitionAppService:
    """
    Application service for the Acquisition bounded context.

    Depends on the EnginePort abstraction to dispatch events; it does NOT
    reach into the engine's internals (event_bus) any more.
    """

    def __init__(self, engine: EnginePort, settings=None) -> None:
        """*settings*: OmniBCISettings，仅用于 status 查询里的设备元数据
        展示。由组合根注入（P2-9，取代方法内延迟 import 全局单例）；
        None = 元数据不可用（单测默认），status 相应字段为 None。"""
        self._engine = engine
        self._settings = settings
        self._connected = False
        self._streaming = False

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def handle_connect(self, cmd: ConnectDataProviderCommand) -> None:
        self._put_event(EventType.ConnectDataProvider)
        self._connected = True
        logger.info("ConnectDataProvider event dispatched")

    def handle_disconnect(self, cmd: DisconnectDataProviderCommand) -> None:
        self._put_event(EventType.DisconnectDataProvider)
        self._connected = False
        self._streaming = False
        logger.info("DisconnectDataProvider event dispatched")

    def handle_start_stream(self, cmd: StartStreamCommand) -> None:
        self._put_event(EventType.StartStream)
        self._streaming = True
        logger.info("StartStream event dispatched")

    def handle_stop_stream(self, cmd: StopStreamCommand) -> None:
        self._put_event(EventType.StopStream)
        self._streaming = False
        logger.info("StopStream event dispatched")

    def reset(self) -> None:
        """Clear connection/stream state.

        These are intent flags held in the API process; they do not track the
        engine's acquisition worker.  When the engine stops, that worker dies,
        so without this the status would stay 'connected/streaming' (stale)
        while the engine panel correctly shows the acquisition process gone.
        """
        self._connected = False
        self._streaming = False

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    def handle_get_status(self, query: GetAcquisitionStatusQuery) -> AcquisitionStatusResult:
        source, sample_rate, channel_count = self._device_metadata()
        return AcquisitionStatusResult(
            connected=self._connected,
            streaming=self._streaming,
            source=source,
            sample_rate=sample_rate,
            channel_count=channel_count,
        )

    def _device_metadata(self):
        """
        Best-effort device metadata from the injected settings (data source
        id, sample rate, channel count).  Returns (None, None, None) when no
        settings were injected — preserving the prior null behaviour.

        P2-9：settings 改为构造注入；配置键缺失经 get(key, None) 显式给
        默认，不再用 except Exception 吞掉所有异常（文件损坏等真故障
        在组合根 import 时即已 fail-fast）。
        """
        if self._settings is None:
            return None, None, None
        source = self._settings.get("ACQUISITION.signal_source", None)
        sample_rate = self._settings.get("ACQUISITION.sample_rate", None)
        channel_count = self._settings.get("ACQUISITION.channel_number", None)
        return (
            str(source) if source is not None else None,
            int(sample_rate) if sample_rate is not None else None,
            int(channel_count) if channel_count is not None else None,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _put_event(self, event_type: EventType, data=None) -> None:
        self._engine.dispatch(Event(event_type, data))
