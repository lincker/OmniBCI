"""Format-independent persistence contract for raw acquisition output."""

from typing import Protocol

from omnibci.domain.signal import SignalChunk


class SignalRecorder(Protocol):
    """One writer owned by one acquisition process; methods run sequentially.

    Receipts are JSON-compatible dictionaries identifying id, session_id, path,
    format, and format_version. Storage layout and encoding belong to the
    implementation. No open file handles should cross the process boundary.
    """

    def start(self, metadata: dict) -> dict:
        """Open a session; identical healthy active metadata is an idempotent retry.

        Reject different active metadata and reuse of closed session IDs.
        """
        ...

    def append(self, chunk: SignalChunk, *, config_revision: str | None = None) -> None:
        """Preserve the complete raw chunk, its identity, and configuration revision."""
        ...

    def flush(self) -> None:
        """Persist accepted chunks without closing the recording; raise on failure."""
        ...

    def finish(self, status: str = "completed", error: str | None = None) -> dict:
        """Flush and close; return status/counts, retaining failures across retries.

        Status is completed, interrupted, or failed. A failed write or flush
        must never later be reported as completed. Repeated finish is idempotent.
        """
        ...
