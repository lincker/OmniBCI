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
Leave ``STIMULATION.auto_spawn = true`` (the default).  The server will
spawn the Stimulation subprocess automatically when
``handle_start_stimulation`` is called from the Web UI.  No extra terminal
required.

In both modes the IPC socket architecture (Stimulation → NeuroEngine) is
unchanged — this class only manages the OS-level process lifecycle.
"""
from __future__ import annotations

import logging
import multiprocessing
from typing import Any
from uuid import uuid4  # noqa: F401 - patched by tests via module attribute

from omnibci.infrastructure.process_lifecycle import (
    ManagedOutputProcessManager,
    run_managed_child,
)

# Managed children must inherit the caller's current environment, not a forkserver's.
mp = multiprocessing.get_context("spawn")


def _configured_paradigm(snapshot: dict) -> str:
    from omnibci.infrastructure.plugin_management.runtime import resolve_configured_plugin

    descriptor, _ = resolve_configured_plugin(snapshot, "stimulation.paradigm", "paradigm")
    return descriptor.selection.canonical_id


def _run_stimulation(
    stop_event: Any = None,
    startup_connection: Any = None,
    *,
    settings_snapshot: dict,
) -> None:
    """Stimulation subprocess entry point.  Runs in the spawned child process."""
    # Spawn does not inherit logging setup. Match the standalone entry point;
    # stderr is also collected by the desktop host. Preserve existing handlers.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    def _create_app(**kwargs):
        from omnibci.infrastructure.stimulation.application import Stimulation
        return Stimulation(**kwargs)

    run_managed_child(
        _create_app,
        component="Stimulation",
        module="stimulation",
        stop_event=stop_event,
        startup_connection=startup_connection,
        settings_snapshot=settings_snapshot,
    )


class StimulationProcessManager(ManagedOutputProcessManager):
    """Manages the Stimulation OS process."""

    _COMPONENT_NAME = "Stimulation"
    _MODULE_NAME = "stimulation"
    _AUTO_SPAWN_KEY = "STIMULATION.auto_spawn"
    _PROCESS_NAME = "StimulationProcess"
    _SELECTION_LABEL = "paradigm"
    _RESOURCE_NOUN = "renderer"
    _STARTUP_TIMEOUT_S = 30.0

    def _configured_selection(self, snapshot: dict) -> str:
        return _configured_paradigm(snapshot)

    def _process_target(self):
        return _run_stimulation

    @property
    def current_paradigm(self) -> str | None:
        """Paradigm the running subprocess was spawned with (None if not ready)."""
        return self._current_selection()
