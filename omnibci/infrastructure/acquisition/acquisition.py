"""Acquisition runtime and worker command contracts."""
import asyncio
import logging
import time
from uuid import uuid4
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from omnibci.application.events import (
    AcquisitionSnapshot,
    CommandFailureKind,
    CommandRequest,
    CommandResult,
    StreamBarrier,
)
from omnibci.domain.acquisition_descriptor import AcquisitionDescriptor
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.domain.signal import SignalChunk
from omnibci.shared.events import Event, EventType

logger = logging.getLogger(__name__)


@runtime_checkable
class CommandQueue(Protocol):
    """Minimal command queue contract consumed by the acquisition worker."""

    def empty(self) -> bool: ...

    def get(self) -> "AcquisitionCommand": ...


@runtime_checkable
class DataSink(Protocol):
    """Minimal downstream sink contract for acquired signal chunks."""

    def put_data(self, chunk: SignalChunk | StreamBarrier) -> None: ...


class AcquisitionCommand(Enum):
    stop_acquisition  = 'stop acquisition'
    get_metadata      = 'get metadata'
    connect           = 'connect'
    start_stream      = 'start stream'
    stop_stream       = 'stop stream'
    stop_and_drain    = 'stop and drain'
    disconnect        = 'disconnect'
    get_checkpoint    = 'get checkpoint'
    restore_checkpoint = 'restore checkpoint'
    update_revision   = 'update revision'


class AcquisitionState(Enum):
    stop    = 'stop'
    running = 'running'


class AcquisitionCommandRejected(RuntimeError):
    """A control command was valid but could not be applied in current state."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_COMMAND_RESULT_NAMES = {
    AcquisitionCommand.connect: EventType.ConnectDataProvider.name,
    AcquisitionCommand.disconnect: EventType.DisconnectDataProvider.name,
    AcquisitionCommand.start_stream: EventType.StartStream.name,
    AcquisitionCommand.stop_stream: EventType.StopStream.name,
    AcquisitionCommand.stop_and_drain: EventType.StopAndDrain.name,
    AcquisitionCommand.get_checkpoint: EventType.GetSourceCheckpoint.name,
    AcquisitionCommand.restore_checkpoint: EventType.RestoreSourceCheckpoint.name,
    AcquisitionCommand.update_revision: EventType.UpdateAcquisitionRevision.name,
    AcquisitionCommand.get_metadata: EventType.GetSourceMetadata.name,
}


class AcquisitionRuntime:
    """
    Runtime: drives a SignalSource to read EEG data and pushes
    SignalChunk objects into the shared DataManager buffer.
    """

    def __init__(
        self,
        data_source: SignalSource,
        command_queue: CommandQueue,
        data_manager: DataSink,
        event_bus=None,
        *,
        source_id: str,
        preparation_required: bool,
        generation: str | None = None,
        config_revision: str | None = None,
    ) -> None:
        self.data_source = data_source
        self.command_queue = command_queue
        # Local worker phase; EventType.AcquisitionState is the API projection event.
        self.status = AcquisitionState.stop
        self.fetch_task: Optional[asyncio.Task] = None
        self.data_manager = data_manager
        self.event_bus = event_bus  # Optional for projection-free unit tests.
        self.should_exit = False
        self.source_id = source_id
        self.preparation_required = preparation_required
        self.generation = generation
        self.config_revision = config_revision
        self._last_sequence = -1
        self._fetch_failure: str | None = None
        self._descriptor = None
        self._raw_metadata: str | None = None
        self._descriptor_error = None
        self.connection_id = uuid4().hex if getattr(data_source, "is_connected", False) else None

    def run(self) -> None:
        asyncio.run(self.serve())

    async def serve(self) -> None:
        try:
            await self.startup()
            await self.main_loop()
        finally:
            await self.shutdown()

    async def startup(self) -> None:
        logger.info('Acquisition started')

    async def shutdown(self) -> None:
        logger.info('Acquisition shutting down')
        if self.data_source.is_connected or self.data_source.is_streaming:
            await self._on_disconnect()
        else:
            await self._cancel_fetch_task()
            self._emit_state()

    async def main_loop(self) -> None:
        while not self.should_exit:
            if not self.command_queue.empty():
                await self.execute(self.command_queue.get())
            await asyncio.sleep(0.01)

    async def execute(self, item) -> None:
        """Execute a command and emit one result for each correlated request."""
        if isinstance(item, tuple):
            command, raw_request = item
        else:
            command, raw_request = item, None
        request = CommandRequest.from_value(raw_request)
        logger.debug('Acquisition command: %s', command)
        if command in (AcquisitionCommand.get_checkpoint, AcquisitionCommand.restore_checkpoint,
                       AcquisitionCommand.update_revision):
            self._configuration_command(command, request)
            return
        handler = {
            AcquisitionCommand.connect:          self._on_connect,
            AcquisitionCommand.disconnect:       self._on_disconnect,
            AcquisitionCommand.start_stream:     self._on_start_stream,
            AcquisitionCommand.stop_stream:      self._on_stop_stream,
            AcquisitionCommand.stop_and_drain:   self._on_stop_stream,
            AcquisitionCommand.stop_acquisition: self._on_stop_acquisition,
            AcquisitionCommand.get_metadata:     self._on_get_metadata,
        }.get(command)
        if handler is None:
            logger.warning('Unknown acquisition command: %s', command)
            if request is not None:
                self._emit_command_result(
                    request,
                    command,
                    ok=False,
                    message=f"Unknown acquisition command: {command!r}",
                    error_code="UNKNOWN_COMMAND",
                    failure_kind=CommandFailureKind.REJECTED,
                )
            return
        try:
            if command is AcquisitionCommand.stop_and_drain and request is None:
                raise AcquisitionCommandRejected("CORRELATION_REQUIRED", "StopAndDrain requires a command ID")
            data = None
            if command is AcquisitionCommand.get_metadata:
                data = await self._on_get_metadata(request)
            elif command is AcquisitionCommand.start_stream:
                await self._on_start_stream(request.payload if request else None)
            else:
                await handler()
            if command is AcquisitionCommand.stop_and_drain:
                self._require_healthy_stream()
                payload = request.payload
                session_id = payload.get("session_id") if isinstance(payload, dict) else None
                if session_id is not None and (not isinstance(session_id, str) or not session_id):
                    raise AcquisitionCommandRejected("INVALID_SESSION_COMMAND", "Invalid drain session ID")
                self.data_manager.put_data(StreamBarrier(
                    request.command_id, self.generation, self.config_revision,
                    self._last_sequence, session_id,
                ))
                # The processor alone can confirm consumption of prior data.
                return
        except asyncio.CancelledError:
            if request is not None:
                self._emit_command_result(
                    request, command, ok=False, message="Command interrupted by acquisition shutdown",
                    error_code="ACQUISITION_CANCELLED", failure_kind=CommandFailureKind.EXECUTION_FAILED,
                )
            raise
        except AcquisitionCommandRejected as exc:
            if request is None:
                return
            logger.warning("Acquisition command %s rejected: %s", command, exc)
            self._emit_command_result(
                request,
                command,
                ok=False,
                message=str(exc),
                error_code=exc.code,
                failure_kind=CommandFailureKind.REJECTED,
            )
            return
        except Exception as exc:
            if request is None:
                raise
            logger.exception("Acquisition command %s execution failed", command)
            self._emit_command_result(
                request,
                command,
                ok=False,
                message=str(exc),
                error_code=type(exc).__name__,
                failure_kind=CommandFailureKind.EXECUTION_FAILED,
            )
            return
        if request is not None:
            self._emit_command_result(request, command, ok=True, data=data)

    def _configuration_command(self, command, request) -> None:
        if request is None:
            return
        try:
            payload = request.payload
            if (not isinstance(payload, dict) or payload.get("generation") != self.generation
                    or payload.get("expected_revision") != self.config_revision):
                raise ValueError("Acquisition command belongs to a different runtime generation or revision")
            if command is AcquisitionCommand.update_revision:
                revision = payload.get("target_revision")
                if not isinstance(revision, str) or len(revision) != 64:
                    raise ValueError("A pinned target revision is required")
                self.config_revision = revision
                data = {}
            else:
                self._require_healthy_stream()
                if self.data_source.is_streaming or self.fetch_task is not None:
                    raise ValueError("Stop and join acquisition before accessing a source checkpoint")
                if command is AcquisitionCommand.get_checkpoint:
                    supported = self.data_source.supports_checkpoint
                    data = {"supported": supported,
                            "checkpoint": self.data_source.checkpoint() if supported else None,
                            "resume_without_checkpoint_safe": getattr(
                                self.data_source, "resume_without_checkpoint_safe", False,
                            ) is True}
                else:
                    if not self.data_source.supports_checkpoint:
                        raise ValueError("The source does not support replay checkpoints")
                    self.data_source.restore_checkpoint(payload.get("checkpoint"))
                    data = {"restored": True}
            data.update(generation=self.generation, config_revision=self.config_revision)
            self._emit_command_result(request, command, ok=True, data=data)
        except Exception as exc:
            self._emit_command_result(request, command, ok=False, message=str(exc),
                                      error_code="ACQUISITION_CONFIGURATION_FAILED",
                                      failure_kind=CommandFailureKind.EXECUTION_FAILED)

    async def _on_connect(self) -> None:
        if self.data_source.is_connected:
            if self.connection_id is None:
                self.connection_id = uuid4().hex
            self.status = AcquisitionState.running
            self._emit_state()
            return
        await self.data_source.connect()
        if not self.data_source.is_connected:
            raise AcquisitionCommandRejected(
                "CONNECT_FAILED",
                "Data source did not enter the connected state",
            )
        self.connection_id = uuid4().hex
        self._descriptor = None
        self._raw_metadata = None
        self._descriptor_error = None
        self.status = AcquisitionState.running
        self._emit_state()

    async def _on_disconnect(self) -> None:
        self.connection_id = None
        self._descriptor = None
        self._raw_metadata = None
        self._descriptor_error = None
        # A successful disconnect means no reader coroutine remains alive.
        # Some SignalSource implementations (including FileReader) do not
        # implicitly stop their stream in disconnect(), so enforce the order
        # here at the worker boundary.
        if self.data_source.is_streaming:
            await self._on_stop_stream()
        else:
            await self._cancel_fetch_task()
        await self.data_source.disconnect()
        if self.data_source.is_connected:
            raise AcquisitionCommandRejected(
                "DISCONNECT_FAILED",
                "Data source remained connected after disconnect",
            )
        self.status = AcquisitionState.stop
        self._emit_state()

    async def _on_start_stream(self, receipt=None) -> None:
        self._require_healthy_stream()
        # start_stream is only honoured once connected (status == running);
        # otherwise it is dropped — but the truth (not streaming) is still
        # pushed to the projection so the API cannot report a phantom stream.
        if not self.data_source.is_connected:
            logger.warning('start_stream ignored: not running (status=%s)', self.status)
            self._emit_state()
            raise AcquisitionCommandRejected(
                "DATA_SOURCE_NOT_CONNECTED",
                "Cannot start stream before the data source is connected",
            )
        if self.preparation_required:
            self._validate_signal_receipt(receipt)
        if self.data_source.is_streaming:
            self._emit_state()
            return
        await self.data_source.start_stream()
        if not self.data_source.is_streaming:
            self._emit_state()
            raise AcquisitionCommandRejected(
                "STREAM_START_FAILED",
                "Data source did not enter the streaming state",
            )
        self.fetch_task = asyncio.create_task(self.fetch())
        logger.info('Fetch task started')
        self._emit_state()

    async def _on_stop_stream(self) -> None:
        if not self.data_source.is_streaming:
            await self._cancel_fetch_task()
            self._emit_state()
            return
        try:
            await self.data_source.stop_stream()
        finally:
            await self._cancel_fetch_task()
        if self.data_source.is_streaming:
            self._emit_state()
            raise AcquisitionCommandRejected(
                "STREAM_STOP_FAILED",
                "Data source remained in the streaming state after stop",
            )
        logger.info('Fetch task stopped')
        self._emit_state()

    async def _cancel_fetch_task(self) -> None:
        """Cancel and join the reader task before confirming a stop command."""
        task = self.fetch_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            if self.fetch_task is task:
                self.fetch_task = None

    async def _on_stop_acquisition(self) -> None:
        self.should_exit = True

    async def _on_get_metadata(self, request=None):
        if not self.data_source.is_connected or self.connection_id is None:
            raise AcquisitionCommandRejected("DATA_SOURCE_NOT_CONNECTED", "Connect the source before reading metadata")
        payload = request.payload if request else None
        refresh = isinstance(payload, dict) and payload.get("refresh") is True
        if self.data_source.is_streaming or self.fetch_task is not None:
            if self._raw_metadata is not None and not refresh:
                return self._metadata_response()
            raise AcquisitionCommandRejected("METADATA_REQUIRES_STOP", "Stop the stream before reading source metadata")
        if self._raw_metadata is None or refresh:
            self._descriptor = self._raw_metadata = None
            self._descriptor_error = None
            try:
                info = await asyncio.wait_for(self.data_source.get_metadata(), timeout=3.0)
            except TimeoutError as exc:
                raise AcquisitionCommandRejected("SOURCE_METADATA_TIMEOUT", "Source metadata request timed out") from exc
            if not isinstance(info, str):
                raise ValueError("Source metadata must be a string")
            self._raw_metadata = info
            try:
                descriptor = self.data_source.describe_output(
                    source_id=self.source_id, connection_id=self.connection_id,
                )
                if not isinstance(descriptor, AcquisitionDescriptor):
                    raise ValueError("Source output description must be an AcquisitionDescriptor")
                if descriptor.source_id != self.source_id or descriptor.connection_id != self.connection_id:
                    raise ValueError("Acquisition descriptor belongs to a different source connection")
                self._descriptor = descriptor
            except Exception as exc:
                self._descriptor_error = str(exc) or "Source cannot describe acquisition output"
        return self._metadata_response()

    def _metadata_response(self):
        return {
            "source_id": self.source_id,
            "connection_id": self.connection_id,
            "metadata": self._raw_metadata,
            "descriptor": self._descriptor.to_dict() if self._descriptor is not None else None,
            "descriptor_error": self._descriptor_error,
        }

    def _validate_signal_receipt(self, receipt) -> None:
        expected = {
            "generation": self.generation, "config_revision": self.config_revision,
            "connection_id": self.connection_id,
            "source_fingerprint": self._descriptor.fingerprint if self._descriptor else None,
        }
        if (not isinstance(receipt, dict) or self._descriptor is None
                or any(receipt.get(key) != value for key, value in expected.items())
                or not isinstance(receipt.get("plan_fingerprint"), str)
                or len(receipt["plan_fingerprint"]) != 64):
            raise AcquisitionCommandRejected("SIGNAL_NOT_PREPARED", "Prepare the signal mapping for this connection and configuration before streaming")

    async def fetch(self) -> None:
        packets_counter = 0
        try:
            while True:
                raw_data = await self.data_source.read_chunk()
                if raw_data is None:
                    # EOF is a clean stream transition and must update the projection.
                    logger.info('Data source end of stream after %d chunks; fetch task finished',
                                packets_counter)
                    await self._finish_stream()
                    break
                chunk = SignalChunk(
                    timestamp=time.time_ns(),
                    raw_data=raw_data,
                    sequence_id=self._last_sequence + 1,
                )
                # TODO: Consider validating data matrices against the acquisition descriptor in the future.
                self.data_manager.put_data(chunk)
                self._last_sequence = chunk.sequence_id
                packets_counter += 1
        except asyncio.CancelledError:
            logger.info('Fetch task cancelled')
        except Exception as exc:
            self._fetch_failure = f"{type(exc).__name__}: {exc}"
            logger.error('Error in fetch task: %s', exc)
            await self._finish_stream()

    def _require_healthy_stream(self) -> None:
        if self._fetch_failure is not None:
            raise RuntimeError(
                "Acquisition lost stream continuity; rebuild the source before resuming "
                f"or creating a checkpoint ({self._fetch_failure})"
            )

    async def _finish_stream(self) -> None:
        """Close the stream from inside fetch() (EOF / fatal error) and
        publish the resulting state."""
        try:
            await self.data_source.stop_stream()
        except Exception as exc:
            self._fetch_failure = self._fetch_failure or f"{type(exc).__name__}: {exc}"
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
                connection_id=self.connection_id,
            ),
        ))

    def _emit_command_result(
        self,
        request: CommandRequest,
        command,
        *,
        ok: bool,
        message: str | None = None,
        error_code: str | None = None,
        failure_kind: CommandFailureKind | None = None,
        data=None,
    ) -> None:
        if self.event_bus is None:
            return
        command_name = _COMMAND_RESULT_NAMES.get(
            command,
            command.name if isinstance(command, Enum) else str(command),
        )
        self.event_bus.put_event(Event(
            EventType.CommandResult,
            CommandResult(
                command_id=request.command_id,
                command=command_name,
                ok=ok,
                message=message,
                error_code=error_code,
                failure_kind=failure_kind,
                data=data,
            ),
        ))
