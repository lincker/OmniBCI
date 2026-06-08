"""
AcquisitionAppService — Application Service.

Translates acquisition commands/queries into EventBus events that the
NeuroEngine already understands.  Keeps acquisition routing logic out of
the routers.
"""
from __future__ import annotations

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

    def __init__(self, engine: EnginePort) -> None:
        self._engine = engine
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

    @staticmethod
    def _device_metadata():
        """
        Best-effort device metadata from settings (data source id, sample rate,
        channel count).  Returns (None, None, None) if settings are unavailable,
        preserving the prior null behaviour rather than raising.
        """
        try:
            from omnibci.conf import settings

            def _get(key):
                try:
                    return settings.get(key)
                except Exception:
                    return None

            source = _get("ACQUISITION.signal_source")
            sample_rate = _get("ACQUISITION.sample_rate")
            channel_count = _get("ACQUISITION.channel_number")
            return (
                str(source) if source is not None else None,
                int(sample_rate) if sample_rate is not None else None,
                int(channel_count) if channel_count is not None else None,
            )
        except Exception:
            return None, None, None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _put_event(self, event_type: EventType, data=None) -> None:
        self._engine.dispatch(Event(event_type, data))
