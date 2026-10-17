"""SQLite archive independent of editable profile files and runtime generation."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from uuid import uuid4


class SQLiteConfigurationArchive:
    """Use a separate connection and tables in the session database.

    Table names and the snapshot's schema_version define this initial archive
    schema; SessionRepository continues to own the database user_version.
    """

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            with self._transaction(write=True) as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS configuration_snapshots (
                        revision TEXT PRIMARY KEY NOT NULL,
                        snapshot TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS configuration_applications (
                        attempt_id TEXT PRIMARY KEY NOT NULL,
                        revision TEXT NOT NULL REFERENCES configuration_snapshots(revision),
                        generation TEXT,
                        context TEXT NOT NULL,
                        session_id TEXT,
                        status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'failed')),
                        staged_at TEXT NOT NULL,
                        resolved_at TEXT,
                        receipt TEXT,
                        error TEXT
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS session_configurations (
                        session_id TEXT NOT NULL,
                        revision TEXT NOT NULL REFERENCES configuration_snapshots(revision),
                        linked_at TEXT NOT NULL,
                        PRIMARY KEY (session_id, revision)
                    )
                """)
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _transaction(self, *, write=False):
        with self._lock:
            if self._closed:
                raise RuntimeError("Configuration archive is closed")
            self._connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    @staticmethod
    def _json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def stage(self, revision, snapshot, *, generation, context, session_id):
        encoded = self._json(snapshot)
        attempt_id, now = str(uuid4()), self._now()
        with self._transaction(write=True) as connection:
            existing = connection.execute(
                "SELECT snapshot FROM configuration_snapshots WHERE revision = ?", (revision,),
            ).fetchone()
            if existing is not None and existing["snapshot"] != encoded:
                raise ValueError(f"Configuration snapshot {revision!r} is immutable")
            connection.execute(
                "INSERT OR IGNORE INTO configuration_snapshots VALUES (?, ?, ?)",
                (revision, encoded, now),
            )
            connection.execute("""
                INSERT INTO configuration_applications
                    (attempt_id, revision, generation, context, session_id, status, staged_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """, (attempt_id, revision, generation, context, session_id, now))
            if session_id is not None:
                self._link_session(session_id, revision)
        return attempt_id

    def confirm(self, attempt_id, revision, *, generation, receipt):
        encoded = self._json(receipt)
        with self._transaction(write=True) as connection:
            row = self._attempt(attempt_id)
            if row["revision"] != revision or row["generation"] not in (None, generation):
                raise ValueError("Configuration acknowledgement identity mismatch")
            if row["status"] == "applied":
                if row["receipt"] != encoded:
                    raise ValueError("Configuration application receipt is immutable")
                return
            if row["status"] != "pending":
                raise ValueError("A failed configuration application cannot become applied")
            connection.execute("""
                UPDATE configuration_applications
                SET generation = ?, status = 'applied', resolved_at = ?, receipt = ?
                WHERE attempt_id = ?
            """, (generation, self._now(), encoded, attempt_id))

    def fail(self, attempt_id, error):
        with self._transaction(write=True) as connection:
            row = self._attempt(attempt_id)
            if row["status"] != "pending":
                return
            connection.execute("""
                UPDATE configuration_applications
                SET status = 'failed', resolved_at = ?, error = ? WHERE attempt_id = ?
            """, (self._now(), error, attempt_id))

    def _attempt(self, attempt_id):
        row = self._connection.execute(
            "SELECT * FROM configuration_applications WHERE attempt_id = ?", (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Configuration application {attempt_id!r} does not exist")
        return row

    def link_session(self, session_id, revision):
        with self._transaction(write=True):
            self._link_session(session_id, revision)

    def _link_session(self, session_id, revision):
        self._connection.execute(
            "INSERT OR IGNORE INTO session_configurations VALUES (?, ?, ?)",
            (session_id, revision, self._now()),
        )

    def get(self, revision):
        with self._transaction():
            return self._get(revision)

    def _get(self, revision):
        row = self._connection.execute(
            "SELECT * FROM configuration_snapshots WHERE revision = ?", (revision,),
        ).fetchone()
        if row is None:
            return None
        applications = []
        for application in self._connection.execute(
            "SELECT * FROM configuration_applications WHERE revision = ? ORDER BY rowid", (revision,),
        ):
            item = dict(application)
            item["receipt"] = json.loads(item["receipt"]) if item["receipt"] is not None else None
            applications.append(item)
        return {"config_revision": revision, "snapshot": json.loads(row["snapshot"]),
                "created_at": row["created_at"], "applications": applications}

    def get_for_session(self, session_id):
        with self._transaction() as connection:
            revisions = connection.execute(
                "SELECT revision FROM session_configurations WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            ).fetchall()
            return [self._get(row["revision"]) for row in revisions]

    def close(self):
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True
