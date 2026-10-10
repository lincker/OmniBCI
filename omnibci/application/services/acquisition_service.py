"""
AcquisitionAppService — Application Service.

Translates acquisition commands/queries into confirmed engine commands.
Keeps acquisition routing and result-waiting logic out of the routers.
"""

import logging
from threading import RLock
from omnibci.application.runtime_admission import admitted
from omnibci.application.plugin_profiles import ProfileRevisionConflict, PluginProfileStorageError
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
from omnibci.application.events import AcquisitionSnapshot, CommandFailureKind, CommandResult, StreamDrainReceipt
from omnibci.application.services.command_result_service import CommandExecutor, CommandRejectedError
from omnibci.domain.montage import SourceMetadata

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
        admission=None,
        lifecycle_lock=None,
    ) -> None:
        self._admission = admission
        """Use injected settings only for optional device status metadata."""
        self._settings = settings
        self._command_executor = command_executor
        # Projection of worker-reported device state (AcquisitionState events);
        # command handlers no longer guess — see on_acquisition_state().
        self._connected = False
        self._streaming = False
        self.lifecycle_lock = lifecycle_lock or RLock()
        self._prepare_signal = None
        self._metadata = None
        self._metadata_binding = None
        self._metadata_recorder = None

    def set_signal_preparer(self, callback) -> None:
        """Install the signal preparation application service after composition."""
        self._prepare_signal = callback

    def set_metadata_recorder(self, binding_provider, recorder) -> None:
        """Persist source observations against the binding that owns the running source."""
        self._metadata_binding = binding_provider
        self._metadata_recorder = recorder

    @property
    def cached_metadata(self) -> dict | None:
        """Return the last confirmed metadata; disconnect invalidates this view."""
        return self._metadata.to_dict() if self._metadata is not None else None

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    @admitted
    def handle_connect(self, cmd: ConnectDataProviderCommand) -> None:
        with self.lifecycle_lock:
            self._put_event(EventType.ConnectDataProvider)
            if self._metadata_recorder is not None:
                try:
                    self.get_metadata(refresh=True, allow_manual_fallback=True)
                except (CommandRejectedError, RuntimeError) as exc:
                    logger.warning("Source connected, but acquisition metadata is unavailable: %s", exc)
                except PluginProfileStorageError as exc:
                    raise PluginProfileStorageError(
                        f"Source connected, but its acquisition montage could not be saved: {exc}"
                    ) from exc
        logger.info("ConnectDataProvider event dispatched")

    @admitted(recovery=True)
    def handle_disconnect(self, cmd: DisconnectDataProviderCommand) -> None:
        with self.lifecycle_lock:
            self._metadata = None
            self._put_event(EventType.DisconnectDataProvider)
        logger.info("DisconnectDataProvider event dispatched")

    @admitted
    def handle_start_stream(self, cmd: StartStreamCommand) -> None:
        with self.lifecycle_lock:
            receipt = self._prepare_signal() if self._prepare_signal is not None else None
            self._put_event(EventType.StartStream, receipt)
        logger.info("StartStream event dispatched")

    @admitted(recovery=True)
    def handle_stop_stream(self, cmd: StopStreamCommand) -> None:
        with self.lifecycle_lock:
            self._put_event(EventType.StopStream)
        logger.info("StopStream event dispatched")

    @admitted(recovery=True)
    def handle_stop_and_drain(
        self, *, session_id: str | None = None, timeout_s: float | None = None,
    ) -> StreamDrainReceipt:
        """Wait for processing evidence after acquisition joins its producer."""
        with self.lifecycle_lock:
            result = self._command_executor.request(
                EventType.StopAndDrain, {"session_id": session_id}, timeout_s=timeout_s,
            )
        try:
            receipt = StreamDrainReceipt.from_value(result.data)
        except ValueError as exc:
            raise RuntimeError("Worker did not provide valid stream drain evidence") from exc
        if session_id is not None and receipt.session_id != session_id:
            raise RuntimeError("Stream drain receipt belongs to a different session")
        return receipt

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
        self._metadata = None

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
        if not snap.connected:
            self._metadata = None

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    @admitted
    def get_metadata(self, *, refresh: bool = False, profile_id: str | None = None,
                     allow_manual_fallback: bool = False) -> dict:
        """Request metadata from the acquisition command loop with a bounded wait."""
        with self.lifecycle_lock:
            if profile_id is not None and self._metadata_binding is None:
                raise ProfileRevisionConflict("Acquisition profile observation storage is unavailable")
            binding = self._metadata_binding(profile_id) if self._metadata_binding is not None else None
            self._metadata = None
            include_observation = self._metadata_recorder is not None or allow_manual_fallback
            payload = {"refresh": refresh}
            if include_observation:
                payload["include_observation"] = True
            result = self._command_executor.request(
                EventType.GetSourceMetadata, payload, timeout_s=5.0,
            )
            value = result.data
            if include_observation:
                if not isinstance(value, dict) or set(value) != {
                    "metadata", "observation", "metadata_error", "observation_error",
                }:
                    raise RuntimeError("Source returned invalid acquisition metadata observation")
                observation = value["observation"]
                if observation is not None and self._metadata_recorder is not None and binding is not None:
                    try:
                        observation = SourceMetadata.from_dict(observation)
                    except (TypeError, ValueError) as exc:
                        raise RuntimeError("Source returned invalid acquisition metadata observation") from exc
                    stored = self._metadata_recorder(binding, observation)
                    if profile_id is not None and stored is None:
                        raise ProfileRevisionConflict("Acquisition profile changed; observation was not saved")
                error = value["metadata_error"] or (value["observation_error"] if not allow_manual_fallback else None)
                if error is not None:
                    raise CommandRejectedError(CommandResult(
                        result.command_id, result.command, False, message=error,
                        error_code="INVALID_SOURCE_METADATA", failure_kind=CommandFailureKind.REJECTED,
                    ))
                value = value["metadata"]
            try:
                metadata = SourceMetadata.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Source returned invalid acquisition metadata") from exc
            self._metadata = metadata
            return metadata.to_dict()

    def handle_get_status(self, query: GetAcquisitionStatusQuery) -> AcquisitionStatusResult:
        source, sample_rate, channel_count = self._device_metadata()
        return AcquisitionStatusResult(
            connected=self._connected,
            streaming=self._streaming,
            source=source,
            sample_rate=sample_rate,
            channel_count=channel_count,
            metadata_available=self._metadata is not None,
            metadata_provenance=self._metadata.provenance if self._metadata else None,
        )

    def _device_metadata(self):
        """
        Best-effort device metadata from the injected settings (data source
        id, sample rate, channel count).  Returns (None, None, None) when no
        settings were injected — preserving the prior null behaviour.

        """
        if self._metadata is not None:
            return self._metadata.source_id, self._metadata.sample_rate, self._metadata.output_row_count
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
