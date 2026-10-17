"""SQLite persistence for session projections and append-only trial history."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from omnibci.domain.repositories import (
    SessionRecord,
    SessionRecordState,
    SessionRepository,
    TrialRecord,
)
from omnibci.domain.session import validate_subject_id


class SQLiteSessionRepository(SessionRepository):
    """Serialize each operation, including mutators, in one database transaction.

    The connection belongs to the main process and may be used by its API and
    event threads. Returned projections are detached from persistent state.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None, timeout=30,
        )
        self._connection.row_factory = sqlite3.Row
        self._closed = False
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._initialize_schema()
        except BaseException:
            self.close()
            raise

    def _initialize_schema(self) -> None:
        with self._transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == self.SCHEMA_VERSION:
                return
            if version != 0:
                raise ValueError(f"Unsupported session database schema version: {version}")
            connection.execute("""
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY NOT NULL,
                    paradigm TEXT,
                    subject_id TEXT,
                    state TEXT NOT NULL CHECK (state IN ('running', 'ended')),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    continuation_of TEXT,
                    config_revision TEXT,
                    interruption_reason TEXT,
                    signal_plan TEXT,
                    recording TEXT
                )
            """)
            connection.execute("CREATE INDEX sessions_subject_id ON sessions(subject_id)")
            connection.execute("""
                CREATE TABLE trials (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    trial_number INTEGER NOT NULL CHECK (trial_number > 0),
                    target_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL,
                    confidence REAL,
                    forced INTEGER NOT NULL CHECK (forced IN (0, 1)),
                    produced_at TEXT NOT NULL,
                    config_revision TEXT,
                    PRIMARY KEY (session_id, trial_number)
                )
            """)
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Session repository is closed")
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def close(self) -> None:
        """Release the connection after application event delivery has stopped."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def save(self, record: SessionRecord) -> None:
        with self._transaction(write=True):
            if self._read(record.session_id) is not None:
                raise ValueError(f"Session {record.session_id!r} already exists; use update()")
            self._write(copy.deepcopy(record), previous=None)

    def update(
        self, session_id: str, mutate: Callable[[SessionRecord], None],
    ) -> Optional[SessionRecord]:
        with self._transaction(write=True):
            previous = self._read(session_id)
            if previous is None:
                return None
            candidate = copy.deepcopy(previous)
            mutate(candidate)
            self._validate_identity(session_id, candidate)
            self._write(candidate, previous)
            return copy.deepcopy(candidate)

    def upsert(
        self,
        session_id: str,
        create: Callable[[], SessionRecord],
        mutate: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        with self._transaction(write=True):
            previous = self._read(session_id)
            candidate = copy.deepcopy(previous) if previous is not None else create()
            self._validate_identity(session_id, candidate)
            mutate(candidate)
            self._validate_identity(session_id, candidate)
            self._write(candidate, previous)
            return copy.deepcopy(candidate)

    def delete(self, session_id: str) -> None:
        with self._transaction(write=True) as connection:
            connection.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))

    def find_by_id(self, session_id: str) -> Optional[SessionRecord]:
        with self._transaction():
            return self._read(session_id)

    def find_all(self) -> list[SessionRecord]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY started_at, rowid",
            ).fetchall()
            return [self._record_from_row(row) for row in rows]

    def find_active(self) -> Optional[SessionRecord]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE state = 'running' "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
            ).fetchone()
            return self._record_from_row(row) if row is not None else None

    @staticmethod
    def _validate_identity(session_id: str, record: SessionRecord) -> None:
        if record.session_id != session_id:
            raise ValueError(f"Session ID must remain {session_id!r}, got {record.session_id!r}")

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.utcoffset() is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="microseconds")

    def _write(self, record: SessionRecord, previous: Optional[SessionRecord]) -> None:
        validate_subject_id(record.subject_id)
        unique_trials: dict[int, TrialRecord] = {}
        for trial in record.trials:
            existing = unique_trials.get(trial.trial_number)
            if existing is not None and existing != trial:
                raise ValueError(f"Conflicting content for trial {trial.trial_number}")
            if existing is None:
                if unique_trials and trial.trial_number <= next(reversed(unique_trials)):
                    raise ValueError("Trial numbers must increase")
                unique_trials[trial.trial_number] = trial
        record.trials = list(unique_trials.values())
        previous_trials = previous.trials if previous is not None else []
        if record.trials[:len(previous_trials)] != previous_trials:
            raise ValueError("Persisted trial history is append-only")
        new_trials = record.trials[len(previous_trials):]
        if previous is not None and previous.state == SessionRecordState.ENDED:
            if record.state != SessionRecordState.ENDED:
                raise ValueError("An ended session cannot return to running")
            if new_trials:
                raise ValueError("Cannot append trials to an ended session")

        values = (
            record.session_id, record.paradigm, record.subject_id, record.state.value,
            self._timestamp(record.started_at),
            self._timestamp(record.ended_at) if record.ended_at is not None else None,
            record.continuation_of, record.config_revision, record.interruption_reason,
            json.dumps(record.signal_plan, allow_nan=False) if record.signal_plan is not None else None,
            json.dumps(record.recording, allow_nan=False) if record.recording is not None else None,
        )
        self._connection.execute("""
            INSERT INTO sessions (
                session_id, paradigm, subject_id, state, started_at, ended_at,
                continuation_of, config_revision, interruption_reason, signal_plan, recording
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                paradigm = excluded.paradigm, subject_id = excluded.subject_id,
                state = excluded.state, started_at = excluded.started_at,
                ended_at = excluded.ended_at, continuation_of = excluded.continuation_of,
                config_revision = excluded.config_revision,
                interruption_reason = excluded.interruption_reason,
                signal_plan = excluded.signal_plan, recording = excluded.recording
        """, values)
        self._connection.executemany("""
            INSERT INTO trials (
                session_id, trial_number, target_id, event_id, confidence, forced,
                produced_at, config_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            (record.session_id, trial.trial_number, trial.target_id, trial.event_id,
             trial.confidence, int(trial.forced), self._timestamp(trial.produced_at), trial.config_revision)
            for trial in new_trials
        ])

    def _read(self, session_id: str) -> Optional[SessionRecord]:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,),
        ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def _record_from_row(self, row: sqlite3.Row) -> SessionRecord:
        trial_rows = self._connection.execute(
            "SELECT * FROM trials WHERE session_id = ? ORDER BY trial_number",
            (row["session_id"],),
        ).fetchall()
        return SessionRecord(
            session_id=row["session_id"], paradigm=row["paradigm"], subject_id=row["subject_id"],
            state=SessionRecordState(row["state"]), started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] is not None else None,
            continuation_of=row["continuation_of"], config_revision=row["config_revision"],
            interruption_reason=row["interruption_reason"],
            signal_plan=json.loads(row["signal_plan"]) if row["signal_plan"] is not None else None,
            recording=json.loads(row["recording"]) if row["recording"] is not None else None,
            trials=[TrialRecord(
                trial_number=trial["trial_number"], target_id=trial["target_id"],
                event_id=trial["event_id"], confidence=trial["confidence"],
                forced=bool(trial["forced"]), produced_at=datetime.fromisoformat(trial["produced_at"]),
                config_revision=trial["config_revision"],
            ) for trial in trial_rows],
        )
