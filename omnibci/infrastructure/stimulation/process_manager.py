#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""StimulationProcessManager — optional subprocess launcher for single-machine deployments.

Multi-machine deployment
------------------------
Set ``STIMULATION.auto_spawn = false`` in settings.toml and start the
stimulation process manually on the display machine:

    python scripts/run_stimulation.py

Single-machine deployment
--------------------------
Leave ``STIMULATION.auto_spawn = true`` (the default).  The backend will
spawn the Stimulation subprocess automatically when
``handle_start_stimulation`` is called from the Web UI.  No extra terminal
required.

In both modes the IPC socket architecture (Stimulation → NeuroEngine) is
unchanged — this class only manages the OS-level process lifecycle.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)


def _run_stimulation(
    paradigm: str | None = None,
    stop_event: Any = None,
) -> None:
    """Stimulation subprocess entry point.  Runs in the spawned child process.

    *paradigm* — when provided, overrides ``STIMULATION.paradigm`` in the
    subprocess's in-memory settings before the Stimulator loads plugins.
    The override is subprocess-local: the settings file on disk is unchanged.
    """
    if paradigm:
        from omnibci.conf import settings
        settings.set("STIMULATION.paradigm", paradigm)
    from omnibci.infrastructure.stimulation.application import Stimulation
    Stimulation(stop_event=stop_event).run()


class StimulationProcessManager:
    """Manages the Stimulation OS process.

    When *auto_spawn* is False all public methods are no-ops, so the same
    ``StimulationAppService`` code path works for both deployment modes.
    """

    _GRACEFUL_TIMEOUT_S = 3.0
    _TERMINATE_TIMEOUT_S = 3.0
    _KILL_TIMEOUT_S = 3.0

    def __init__(self, auto_spawn: bool = True) -> None:
        self._auto_spawn = auto_spawn
        self._process: mp.Process | None = None
        self._stop_event: Any = None
        # Paradigm the *running* subprocess was spawned with (None = config default).
        self._paradigm: str | None = None
        # Synchronous API routes may run concurrently in a thread pool.  The
        # re-entrant lock also permits start() to reuse the single stop path
        # when a paradigm change requires a restart.
        self._lifecycle_lock = RLock()

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self, paradigm: str | None = None) -> None:
        """Spawn the Stimulation subprocess (no-op if auto_spawn=False or already running).

        *paradigm* is forwarded to the subprocess and overrides the
        ``STIMULATION.paradigm`` config value in-process only.
        """
        with self._lifecycle_lock:
            if not self._auto_spawn:
                logger.debug(
                    "auto_spawn=False — Stimulation process must be started manually"
                )
                return
            if self._process is not None:
                process_alive = self._process.is_alive()
                if process_alive and paradigm == self._paradigm:
                    logger.debug(
                        "Stimulation process already running requested paradigm "
                        "(pid=%s, paradigm=%s)",
                        self._process.pid,
                        paradigm or "<config>",
                    )
                    return
                if process_alive:
                    logger.info(
                        "Stimulation paradigm change %s → %s; restarting subprocess",
                        self._paradigm or "<config>",
                        paradigm or "<config>",
                    )
                else:
                    logger.info(
                        "Reaping exited Stimulation subprocess before restart "
                        "(pid=%s, exitcode=%s)",
                        self._process.pid,
                        self._process.exitcode,
                    )
                self._stop_locked()

            stop_event = mp.Event()
            process = mp.Process(
                target=_run_stimulation,
                args=(paradigm, stop_event),
                name="StimulationProcess",
                daemon=True,
            )
            process.start()
            self._process = process
            self._stop_event = stop_event
            self._paradigm = paradigm
            logger.info(
                "Stimulation subprocess spawned (pid=%s, paradigm=%s)",
                process.pid,
                paradigm or "<config>",
            )

    def stop(self) -> None:
        """Cooperatively stop, escalating without ever losing a live handle."""
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if not self._auto_spawn:
            return
        process = self._process
        if process is None:
            return

        pid = process.pid
        if process.is_alive():
            if self._stop_event is not None:
                self._stop_event.set()
            process.join(timeout=self._GRACEFUL_TIMEOUT_S)

        if process.is_alive():
            logger.warning(
                "Stimulation subprocess did not stop gracefully; terminating "
                "(pid=%s)",
                pid,
            )
            process.terminate()
            process.join(timeout=self._TERMINATE_TIMEOUT_S)

        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                logger.warning(
                    "Stimulation subprocess ignored terminate; killing (pid=%s)",
                    pid,
                )
                kill()
                process.join(timeout=self._KILL_TIMEOUT_S)

        if process.is_alive():
            message = (
                "Stimulation subprocess is still alive after graceful stop, "
                f"terminate, and kill escalation (pid={pid}); refusing to "
                "discard its handle or start another renderer"
            )
            logger.error(message)
            raise RuntimeError(message)

        logger.info(
            "Stimulation subprocess stopped (pid=%s, exitcode=%s)",
            pid,
            process.exitcode,
        )
        close = getattr(process, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # pragma: no cover - platform defensive
                logger.warning(
                    "Failed to close stopped Stimulation process handle "
                    "(pid=%s): %s",
                    pid,
                    exc,
                )
        self._process = None
        self._stop_event = None
        self._paradigm = None

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            return self._process is not None and self._process.is_alive()

    @property
    def current_paradigm(self) -> str | None:
        """Paradigm the running subprocess was spawned with (None if stopped or config default)."""
        with self._lifecycle_lock:
            return (
                self._paradigm
                if self._process is not None and self._process.is_alive()
                else None
            )
