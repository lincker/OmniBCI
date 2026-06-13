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

logger = logging.getLogger(__name__)


def _run_stimulation() -> None:
    """Stimulation subprocess entry point.  Runs in the spawned child process."""
    from omnibci.infrastructure.stimulation.application import Stimulation
    Stimulation().run()


class StimulationProcessManager:
    """Manages the Stimulation OS process.

    When *auto_spawn* is False all public methods are no-ops, so the same
    ``StimulationAppService`` code path works for both deployment modes.
    """

    def __init__(self, auto_spawn: bool = True) -> None:
        self._auto_spawn = auto_spawn
        self._process: mp.Process | None = None

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the Stimulation subprocess (no-op if auto_spawn=False or already running)."""
        if not self._auto_spawn:
            logger.debug("auto_spawn=False — Stimulation process must be started manually")
            return
        if self.is_alive:
            logger.debug("Stimulation process already running (pid=%s)", self._process.pid)
            return
        self._process = mp.Process(
            target=_run_stimulation,
            name="StimulationProcess",
            daemon=True,
        )
        self._process.start()
        logger.info("Stimulation subprocess spawned (pid=%s)", self._process.pid)

    def stop(self) -> None:
        """Terminate the Stimulation subprocess if it is running."""
        if not self._auto_spawn:
            return
        if self._process is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=3.0)
            logger.info("Stimulation subprocess stopped (pid=%s)", self._process.pid)
        self._process = None

    @property
    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()
