"""
Acquisition domain ports.

  SignalSource   — abstraction over an EEG signal source (device or file);
  TriggerEmitter — abstraction over sending a trigger/event to an
                   external device.
"""
from abc import ABC, abstractmethod
from typing import Any, Optional

from numpy import typing as npt

class SignalSource(ABC):
    """
    Abstract class for signal source.

    This class defines the interface for signal source, which are responsible for
    reading data from a source and providing it in a standardized format.
    """
    def __init__(self):
        super().__init__()
        self._is_connected = False
        self._is_streaming = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    @property
    def is_streaming(self) -> bool:
        return self._is_streaming

    @property
    def supports_checkpoint(self) -> bool:
        """Whether a stopped source can resume its next undelivered sample."""
        return False

    def checkpoint(self) -> dict[str, Any]:
        """Capture a stopped source after its acquisition fetch task has joined.

        An optional source_identity envelope has string resource_id and revision
        fields. It distinguishes a new input from changed content at the same
        input, while the remaining checkpoint state stays opaque to the host.
        """
        raise NotImplementedError("This signal source does not support checkpoints")

    def restore_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Stage a checkpoint for the next start; unsupported for live sources."""
        raise NotImplementedError("This signal source does not support checkpoints")

    @abstractmethod
    async def get_metadata(self):
        """get metadata"""

    @abstractmethod
    async def connect(self) -> bool:
        """setup connection"""

    @abstractmethod
    async def start_stream(self) -> bool:
        """start reading data"""

    @abstractmethod
    async def read_chunk(self) -> Optional[npt.NDArray[Any]]:
        """Read a fixed sized chunk.

        Returns None to signal a clean end of stream (e.g. file playback
        reached EOF); live device sources simply never return None.
        """

    @abstractmethod
    async def stop_stream(self) -> bool:
        """stop reading data"""

    @abstractmethod
    async def disconnect(self) -> bool:
        """teardown connection"""


class TriggerEmitter(ABC):
    @abstractmethod
    def send(self, event_id: int) -> bool:
        """send trigger/event"""

    @abstractmethod
    def clear(self) -> None:
        """clear trigger/event, get read for sending event next time"""
