"""Reconcile persisted sessions with inactive, format-specific raw recordings."""

from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path

from omnibci.domain.repositories import SessionRecord, SessionRecordState, SessionRepository
from omnibci.infrastructure.acquisition.recording import (
    FORMAT, FORMAT_VERSION, LocalSignalRecorder, recover_recording,
)
from omnibci.infrastructure.storage_lock import StorageInUseError, StorageLease


RESTART_REASON = "Backend restarted before the session ended"


def _recording_path(record: SessionRecord) -> Path | None:
    value = (record.recording or {}).get("path")
    return Path(value).expanduser() if isinstance(value, str) and value else None


def _candidate_paths(record: SessionRecord) -> list[Path]:
    path = _recording_path(record)
    if path is not None:
        return [path]
    info = record.recording or {}
    if info.get("status") != "pending" and not info.get("recovery_error"):
        return []
    # Recover only an existing session's lost acknowledgement. No Session or
    # start time is inferred from otherwise orphaned directories.
    root = LocalSignalRecorder().root
    # A child may own its directory before the first manifest replacement.
    # With no acknowledged path, identity cannot safely narrow this check.
    # Independent active backends must use separate recording roots.
    with ExitStack() as leases:
        for writer_lock in sorted(root.glob("*/.writer.lock")):
            leases.enter_context(StorageLease(writer_lock))
    matches = []
    for manifest in root.glob("*/metadata.json"):
        try:
            with manifest.open(encoding="utf-8") as stream:
                metadata = json.load(stream)
        except (OSError, ValueError):
            continue
        if isinstance(metadata, dict) and metadata.get("session_id") == record.session_id:
            matches.append(manifest.parent)
    return sorted(matches)


def recover_session_recording(record: SessionRecord) -> dict | None:
    """Recover a referenced recording after runtime shutdown; never invent data.

    Ownership conflicts propagate so callers cannot release the session while an
    orphan acquisition worker is still writing. Other failures remain explicit
    in the stored descriptor, keeping its original identity and location.
    """
    if record.recording is None:
        return None
    original = deepcopy(record.recording)
    try:
        candidates = _candidate_paths(record)
        with ExitStack() as leases:
            for candidate in candidates:
                if candidate.is_absolute() and candidate.is_dir():
                    leases.enter_context(StorageLease(candidate / ".writer.lock"))
        if not candidates:
            raise ValueError("Recording location was not acknowledged and no unique session recording was found")
        if len(candidates) != 1:
            raise ValueError("Multiple recording directories match this session; automatic recovery is ambiguous")
        path = candidates[0]
        if not path.is_absolute():
            raise ValueError("Recording recovery requires an absolute directory path")
        if not path.is_dir():
            raise FileNotFoundError(f"Recording directory is unavailable: {path}")
        format_id, version = original.get("format", FORMAT), original.get("format_version", FORMAT_VERSION)
        if format_id != FORMAT or type(version) is not int or version != FORMAT_VERSION:
            raise ValueError(f"Unsupported recording recovery format/version: {format_id!r}/{version!r}")
        recovered = recover_recording(
            path, expected_session_id=record.session_id, expected_recording_id=original.get("id"),
        )
    except StorageInUseError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        return {**original, "status": "failed", "recovery_error": str(exc)}
    result = {**original, **recovered}
    result.pop("recovery_error", None)
    if original.get("status") == "failed":
        result["status"] = "failed"
        if original.get("error") is not None:
            result["error"] = original["error"]
    return result


def reconcile_sessions(repository: SessionRepository) -> None:
    """End stale sessions only after every referenced writer passes preflight.

    The composition root must hold the database lease and call this before
    starting workers or accepting commands. No global directory scan imports
    orphan sessions, because early recordings do not contain session start times.
    A pending existing session may discover its unique lost file acknowledgement.
    """
    records = [record for record in repository.find_all()
               if record.state is SessionRecordState.RUNNING
               or (record.recording or {}).get("status") in {"pending", "recording"}]
    with ExitStack() as leases:
        paths = set()
        for record in records:
            try:
                paths.update(_candidate_paths(record))
            except (OSError, ValueError):
                # The per-record recovery below retains the diagnostic.
                pass
        for path in sorted(paths, key=str):
            if path.is_absolute() and path.is_dir():
                leases.enter_context(StorageLease(path / ".writer.lock"))
    # Collect first, so a late ownership conflict cannot partially close records.
    recovered = [(record, recover_session_recording(record)) for record in records]
    for previous, recording in recovered:
        def reconcile(candidate):
            if candidate.state is SessionRecordState.RUNNING:
                candidate.mark_ended(interruption_reason=RESTART_REASON)
            candidate.recording = recording

        repository.update(previous.session_id, reconcile)
