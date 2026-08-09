"""Acquisition domain service and worker command contracts."""
import asyncio
import logging
import time
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from omnibci.application.events import (
    AcquisitionSnapshot,
    CommandFailureKind,
    CommandRequest,
    CommandResult,
)
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
}


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
        # Local worker phase; EventType.AcquisitionState is the API projection event.
        self.status = AcquisitionState.stop
        self.fetch_task: Optional[asyncio.Task] = None
        self.data_manager = data_manager
        self.event_bus = event_bus  # Optional for projection-free unit tests.
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
            await handler()
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
            self._emit_command_result(request, command, ok=True)

    async def _on_connect(self) -> None:
        if self.data_source.is_connected:
            self.status = AcquisitionState.running
            self._emit_state()
            return
        await self.data_source.connect()
        if not self.data_source.is_connected:
            raise AcquisitionCommandRejected(
                "CONNECT_FAILED",
                "Data source did not enter the connected state",
            )
        self.status = AcquisitionState.running
        self._emit_state()

    async def _on_disconnect(self) -> None:
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

    async def _on_start_stream(self) -> None:
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
        await self.data_source.stop_stream()
        if self.data_source.is_streaming:
            self._emit_state()
            raise AcquisitionCommandRejected(
                "STREAM_STOP_FAILED",
                "Data source remained in the streaming state after stop",
            )
        if self.fetch_task:
            await self._cancel_fetch_task()
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
            pass
        finally:
            if self.fetch_task is task:
                self.fetch_task = None

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
                    # EOF is a clean stream transition and must update the projection.
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

    def _emit_command_result(
        self,
        request: CommandRequest,
        command,
        *,
        ok: bool,
        message: str | None = None,
        error_code: str | None = None,
        failure_kind: CommandFailureKind | None = None,
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
            ),
        ))
