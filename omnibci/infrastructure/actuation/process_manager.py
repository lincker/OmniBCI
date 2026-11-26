"""ActuationProcessManager — optional subprocess launcher for single-machine deployments.

Multi-machine deployment
------------------------
Set ``ACTUATION.auto_spawn = false`` in settings.toml and start the actuation
process manually on the device machine:

    python scripts/run_actuation.py

Single-machine deployment
--------------------------
Leave ``ACTUATION.auto_spawn = true``. The server spawns the subprocess when
Actuation starts and reaps it on Actuation Stop or server shutdown.

In both modes the IPC socket architecture (Actuation → server IPC) is
unchanged — this class only manages the OS-level process lifecycle. Mirrors
StimulationProcessManager.
"""
from __future__ import annotations

import multiprocessing
from typing import Any
from uuid import uuid4  # noqa: F401 - patched by tests via module attribute

from omnibci.infrastructure.plugin_management.preparation import configured_bindings
from omnibci.infrastructure.process_lifecycle import (
    ManagedOutputProcessManager,
    run_managed_child,
)

# Managed children must inherit the caller's current environment, not a forkserver's.
mp = multiprocessing.get_context("spawn")


def _configured_effector(snapshot: dict) -> str | None:
    bindings = configured_bindings(snapshot, slot="actuation.effector")
    if not bindings:
        return None
    return bindings[0].selection.canonical_id


def _run_actuation(
    stop_event: Any = None,
    startup_connection: Any = None,
    *,
    settings_snapshot: dict,
) -> None:
    """Actuation subprocess entry point.  Runs in the spawned child process."""
    def _create_app(**kwargs):
        from omnibci.infrastructure.actuation.application import Actuation
        return Actuation(**kwargs)

    run_managed_child(
        _create_app,
        component="Actuation",
        module="actuation",
        stop_event=stop_event,
        startup_connection=startup_connection,
        settings_snapshot=settings_snapshot,
    )


class ActuationProcessManager(ManagedOutputProcessManager):
    """Manages the Actuation OS process."""

    _COMPONENT_NAME = "Actuation"
    _MODULE_NAME = "actuation"
    _AUTO_SPAWN_KEY = "ACTUATION.auto_spawn"
    _PROCESS_NAME = "ActuationProcess"
    _SELECTION_LABEL = "effector"
    _RESOURCE_NOUN = "controller"
    _STARTUP_TIMEOUT_S = 10.0

    def _configured_selection(self, snapshot: dict) -> str | None:
        return _configured_effector(snapshot)

    def _process_target(self):
        return _run_actuation

    @property
    def current_effector(self) -> str | None:
        """Effector the running subprocess was spawned with (None if stopped or config default)."""
        return self._current_selection()
