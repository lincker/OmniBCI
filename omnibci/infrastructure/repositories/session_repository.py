"""
InMemorySessionRepository — Infrastructure Adapter.

Implements SessionRepository using a plain dict protected by a threading.Lock.
Suitable for development and single-process deployment.

Thread-safety contract:
  Every public method acquires the lock for the full duration of the
  operation so callers in different threads (EventBus callback thread vs.
  uvicorn asyncio thread) see a consistent view.

Swap strategy:
  Replace this class with a SQLite / Postgres implementation that satisfies
  the same SessionRepository interface — SessionAppService requires no changes.
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

from omnibci.domain.repositories import SessionRecord, SessionRepository


class InMemorySessionRepository(SessionRepository):
    """Thread-safe in-memory implementation of SessionRepository."""

    def __init__(self) -> None:
        self._store: Dict[str, SessionRecord] = {}
        self._insertion_order: List[str] = []   # preserves find_all() order
        self._active_id: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def save(self, record: SessionRecord) -> None:
        """
        Insert or replace.  On insert, marks this record as the active session
        if its state is RUNNING.
        """
        with self._lock:
            is_new = record.session_id not in self._store
            self._store[record.session_id] = record
            if is_new:
                self._insertion_order.append(record.session_id)
            # Track active: newest RUNNING session
            if record.state.value == "running":
                self._active_id = record.session_id
            elif self._active_id == record.session_id:
                # This was the active session and is now ended
                self._active_id = self._find_latest_running()

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)
            try:
                self._insertion_order.remove(session_id)
            except ValueError:
                pass
            if self._active_id == session_id:
                self._active_id = self._find_latest_running()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def find_by_id(self, session_id: str) -> Optional[SessionRecord]:
        with self._lock:
            record = self._store.get(session_id)
            return copy.deepcopy(record) if record is not None else None

    def find_all(self) -> List[SessionRecord]:
        with self._lock:
            return [copy.deepcopy(self._store[sid])
                    for sid in self._insertion_order
                    if sid in self._store]

    def find_active(self) -> Optional[SessionRecord]:
        with self._lock:
            if self._active_id is None:
                return None
            record = self._store.get(self._active_id)
            return copy.deepcopy(record) if record is not None else None

    # ------------------------------------------------------------------
    # Internal helpers (must be called with lock held)
    # ------------------------------------------------------------------

    def _find_latest_running(self) -> Optional[str]:
        """Return the most recently inserted RUNNING session id, or None."""
        for sid in reversed(self._insertion_order):
            rec = self._store.get(sid)
            if rec and rec.state.value == "running":
                return sid
        return None
