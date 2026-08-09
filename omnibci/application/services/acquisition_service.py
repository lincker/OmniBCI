"""
AcquisitionAppService — Application Service.

Translates acquisition commands/queries into confirmed engine commands.
Keeps acquisition routing and result-waiting logic out of the routers.
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
from omnibci.application.events import AcquisitionSnapshot
from omnibci.application.services.command_result_service import CommandExecutor

logger = logging.getLogger(__name__)


class AcquisitionAppService:
    """
    Application service for the Acquisition bounded context.

    Depends on the narrower CommandExecutor port; every write returns only
    after a terminal worker result. It never reaches into engine internals.
    """

    def __init__(
        self,
        command_executor: CommandExecutor,
        settings=None,
    ) -> None:
        """Use injected settings only for optional device status metadata."""
        self._settings = settings
        self._command_executor = command_executor
        # Projection of worker-reported device state (AcquisitionState events);
        # command handlers no longer guess — see on_acquisition_state().
        self._connected = False
        self._streaming = False

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def handle_connect(self, cmd: ConnectDataProviderCommand) -> None:
        self._put_event(EventType.ConnectDataProvider)
        logger.info("ConnectDataProvider event dispatched")

    def handle_disconnect(self, cmd: DisconnectDataProviderCommand) -> None:
        self._put_event(EventType.DisconnectDataProvider)
        logger.info("DisconnectDataProvider event dispatched")

    def handle_start_stream(self, cmd: StartStreamCommand) -> None:
        self._put_event(EventType.StartStream)
        logger.info("StartStream event dispatched")

    def handle_stop_stream(self, cmd: StopStreamCommand) -> None:
        self._put_event(EventType.StopStream)
        logger.info("StopStream event dispatched")

    def reset(self) -> None:
        """Clear the state projection.

        The projection is fed by AcquisitionState events from the acquisition
        worker.  When the engine stops, that worker is killed and emits no
        final event, so without this the status would stay 'connected/
        streaming' (stale) while the engine panel correctly shows the
        acquisition process gone.
        """
        self._connected = False
        self._streaming = False

    # ------------------------------------------------------------------
    # EventBus listeners (registered in bootstrap.register_engine_listeners)
    # ------------------------------------------------------------------

    def on_acquisition_state(self, event: Event) -> None:
        """Receive EventType.AcquisitionState and update the projection with
        the worker-reported authoritative device state."""
        snap = event.data
        if not isinstance(snap, AcquisitionSnapshot):
            logger.error("AcquisitionState payload is %s, not AcquisitionSnapshot",
                         type(snap).__name__)
            return
        self._connected = snap.connected
        self._streaming = snap.streaming

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
        self._command_executor.request(event_type, data)
