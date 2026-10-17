"""Synchronous session boundary used by the signal-processing worker."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from omnibci.application.events import StreamDrainReceipt
from omnibci.domain.classification import Classification
from omnibci.domain.epoch import EpochInfo
from omnibci.domain.events import TrialCompleted


@dataclass(frozen=True)
class DecodedTrial:
    """One completed epoch's metadata and result, without its signal matrix."""

    epoch_info: EpochInfo
    classification: Classification
    config_revision: str | None = None


class ProcessingSession(Protocol):
    """Session operations execute on the same producer as drain acknowledgments."""

    def record_trial(self, result: DecodedTrial) -> TrialCompleted | None:
        """Publish before returning; only an inactive session may discard a result."""
        ...

    def start_session(self, payload, *, config_revision: str | None,
                      before_end: Callable[[str], None]) -> bool:
        """Validate identity, then cancel pending configuration before replacement."""
        ...

    def end_session(self, payload, *, before_end: Callable[[str], None]) -> bool:
        """End only the matching identity; stale commands have no side effects."""
        ...

    def end_current_session(self, *, before_end: Callable[[str], None]) -> None:
        """Cancel pending configuration and publish the current session's end."""
        ...

    def validate_drain(self, session_id: str | None) -> str | None:
        """Reject a mismatched session and return the running identity, if any."""
        ...

    def drain_receipt(self, *, generation: str | None, config_revision: str | None,
                      last_sequence: int, interrupted_epoch: bool) -> StreamDrainReceipt:
        """Report the aggregate's completed count after synchronous publication."""
        ...
