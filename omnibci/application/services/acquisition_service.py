"""
AcquisitionAppService — Application Service.

Translates acquisition commands/queries into confirmed engine commands.
Keeps acquisition routing and result-waiting logic out of the routers.
"""

import logging
from threading import RLock
from copy import deepcopy
from omnibci.application.runtime_admission import admitted
from omnibci.application.processing_source import acquisition_binding_key
from omnibci.application.source_expectations import compare_source_expectations

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
from omnibci.domain.signal_info import SignalInfo

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
        self._connection_id = None
        self.lifecycle_lock = lifecycle_lock or RLock()
        self._prepare_signal = None
        self._metadata_binding = None
        self._clear_metadata()

    def set_signal_preparer(self, callback) -> None:
        """Install the signal preparation application service after composition."""
        self._prepare_signal = callback

    def set_metadata_binding(self, binding_provider) -> None:
        """Scope connection metadata to the binding that owns the running source."""
        self._metadata_binding = binding_provider

    @property
    def cached_descriptor(self) -> dict | None:
        """Return the last confirmed metadata; disconnect invalidates this view."""
        return self._descriptor.to_dict() if self._descriptor is not None else None

    @property
    def is_streaming(self) -> bool:
        """Return the most recent worker-reported streaming state."""
        return self._streaming

    def cached_descriptor_for(self, binding: dict | None) -> dict | None:
        """Return an observation only for the acquisition selection that owns it."""
        with self.lifecycle_lock:
            if acquisition_binding_key(binding) != self._descriptor_owner:
                return None
            return self.cached_descriptor

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    @admitted
    def handle_connect(self, cmd: ConnectDataProviderCommand) -> None:
        with self.lifecycle_lock:
            self._put_event(EventType.ConnectDataProvider)
        logger.info("ConnectDataProvider event dispatched")

    @admitted(recovery=True)
    def handle_disconnect(self, cmd: DisconnectDataProviderCommand) -> None:
        with self.lifecycle_lock:
            self._clear_metadata()
            self._put_event(EventType.DisconnectDataProvider)
        logger.info("DisconnectDataProvider event dispatched")

    @admitted
    def handle_start_stream(self, cmd: StartStreamCommand) -> None:
        with self.lifecycle_lock:
            if not self._streaming:
                # EOF and disconnect stop the source before processing finishes.
                self.handle_stop_and_drain()
            receipt = self._prepare_signal() if self._prepare_signal is not None else None
            self._put_event(EventType.StartStream, receipt)
        logger.info("StartStream event dispatched")

    @admitted(recovery=True)
    def handle_stop_stream(self, cmd: StopStreamCommand) -> None:
        self.handle_stop_and_drain()
        logger.info("Stream stopped and processing drained")

    @admitted(recovery=True)
    def handle_stop_and_drain(
        self, *, timeout_s: float | None = None,
    ) -> StreamDrainReceipt:
        """Wait for processing evidence after acquisition joins its producer."""
        with self.lifecycle_lock:
            result = self._command_executor.request(
                EventType.StopAndDrain, timeout_s=timeout_s,
            )
        try:
            receipt = StreamDrainReceipt.from_value(result.data)
        except ValueError as exc:
            raise RuntimeError("Worker did not provide valid stream drain evidence") from exc
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
        self._connection_id = None
        self._clear_metadata()

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
        if not snap.connected or snap.connection_id != self._connection_id:
            self._clear_metadata()
        self._connection_id = snap.connection_id if snap.connected else None

    # ------------------------------------------------------------------
    # Query handlers
    # ------------------------------------------------------------------

    @admitted
    def get_metadata(self, *, refresh: bool = False) -> dict:
        """Read plugin metadata for the current connection without saving a profile."""
        with self.lifecycle_lock:
            self._read_metadata(refresh=refresh)
            return deepcopy(self._raw_metadata)

    @admitted
    def get_descriptor(self, *, refresh: bool = False) -> dict:
        """Return the internal signal description required to prepare processing."""
        with self.lifecycle_lock:
            result = self._read_metadata(refresh=refresh)
            if self._descriptor is None:
                raise CommandRejectedError(CommandResult(
                    result.command_id, result.command, False,
                    message=result.data["descriptor_error"] or "Source does not provide a valid acquisition descriptor",
                    error_code="INVALID_ACQUISITION_DESCRIPTOR", failure_kind=CommandFailureKind.REJECTED,
                ))
            return self.cached_descriptor

    def _read_metadata(self, *, refresh: bool):
        binding = self._metadata_binding() if self._metadata_binding is not None else None
        self._clear_metadata()
        result = self._command_executor.request(
            EventType.GetSourceMetadata, {"refresh": refresh}, timeout_s=5.0,
        )
        value = result.data
        if (not isinstance(value, dict) or set(value) != {
                "source_id", "connection_id", "metadata", "descriptor", "descriptor_error"}
                or not isinstance(value["source_id"], str) or not value["source_id"]
                or not isinstance(value["connection_id"], str) or not value["connection_id"]
                or not isinstance(value["metadata"], str)
                or value["descriptor_error"] is not None and not isinstance(value["descriptor_error"], str)):
            raise RuntimeError("Source returned invalid acquisition metadata")
        metadata = None
        if value["descriptor"] is not None:
            try:
                metadata = SignalInfo.from_dict(value["descriptor"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Source returned invalid acquisition descriptor") from exc
            if (metadata.source_id != value["source_id"]
                    or metadata.connection_id != value["connection_id"]):
                raise RuntimeError("Acquisition descriptor belongs to a different source connection")
        self._descriptor = metadata
        self._raw_metadata = deepcopy({key: value[key] for key in ("source_id", "connection_id", "metadata", "descriptor")})
        self._raw_metadata["comparison"] = (
            compare_source_expectations(binding, metadata) if metadata is not None else None
        )
        self._descriptor_owner = acquisition_binding_key(binding)
        self._connection_id = value["connection_id"]
        return result

    def handle_get_status(self, query: GetAcquisitionStatusQuery) -> AcquisitionStatusResult:
        source, sample_rate, channel_count = self._device_metadata()
        provenance = self._descriptor.provenance if self._descriptor is not None else None
        return AcquisitionStatusResult(
            connected=self._connected,
            streaming=self._streaming,
            source=source,
            sample_rate=sample_rate,
            channel_count=channel_count,
            metadata_available=self._raw_metadata is not None,
            metadata_provenance=provenance,
            connection_id=self._connection_id,
        )

    def _device_metadata(self):
        """Show source dimensions only after the connected plugin reports them."""
        if self._raw_metadata is None:
            source = self._settings.get("ACQUISITION.signal_source") if self._settings is not None else None
            return str(source) if source is not None else None, None, None
        source = self._raw_metadata["source_id"]
        if self._descriptor is None or self._descriptor.provenance == "user-declared":
            return source, None, None
        return source, self._descriptor.sample_rate, self._descriptor.signal_channel_count

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _clear_metadata(self) -> None:
        self._descriptor = None
        self._raw_metadata = None
        self._descriptor_owner = None

    def _put_event(self, event_type: EventType, data=None) -> None:
        self._command_executor.request(event_type, data)
