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
from copy import deepcopy
from threading import RLock
from typing import Any

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessState,
    StartupReporter,
    wait_for_startup_report,
)

logger = logging.getLogger(__name__)


def _run_stimulation(
    paradigm: str | None = None,
    stop_event: Any = None,
    startup_connection: Any = None,
    *,
    settings_snapshot: dict | None = None,
) -> None:
    """Stimulation subprocess entry point.  Runs in the spawned child process.

    A pinned snapshot is required and installed before runtime imports.
    """
    reporter = (
        StartupReporter(
            startup_connection, component="Stimulation",
            config_revision=(settings_snapshot or {}).get("CONFIG.revision"),
            generation=(settings_snapshot or {}).get("CONFIG.generation"),
        )
        if startup_connection is not None
        else None
    )
    try:
        if settings_snapshot is None:
            raise RuntimeError("Stimulation requires a pinned settings snapshot")
        from omnibci.conf import settings
        selected = settings_snapshot.get("STIMULATION.paradigm")
        if paradigm is not None and paradigm != selected:
            raise RuntimeError("Apply the paradigm configuration before starting Stimulation")
        settings.use_snapshot(settings_snapshot)
        from omnibci.infrastructure.stimulation.application import Stimulation
        kwargs = {"stop_event": stop_event, "settings_snapshot": settings_snapshot}
        if reporter is not None:
            kwargs["startup_reporter"] = reporter
        Stimulation(**kwargs).run()
    except BaseException as exc:
        if reporter is not None:
            reporter.failed("entrypoint", exc)
        raise
    finally:
        if reporter is not None:
            reporter.close()


class StimulationProcessManager:
    """Manages the Stimulation OS process.

    When *auto_spawn* is False all public methods are no-ops, so the same
    ``StimulationAppService`` code path works for both deployment modes.
    """

    _GRACEFUL_TIMEOUT_S = 3.0
    _TERMINATE_TIMEOUT_S = 3.0
    _KILL_TIMEOUT_S = 3.0
    _STARTUP_TIMEOUT_S = 30.0

    def __init__(
        self,
        auto_spawn: bool = True,
        *,
        startup_timeout_s: float = _STARTUP_TIMEOUT_S,
    ) -> None:
        if startup_timeout_s <= 0:
            raise ValueError("startup_timeout_s must be greater than zero")
        self._default_auto_spawn = auto_spawn
        self._auto_spawn = auto_spawn
        self._settings_snapshot: dict | None = None
        self._startup_timeout_s = float(startup_timeout_s)
        self._process: mp.Process | None = None
        self._stop_event: Any = None
        self._startup_receiver: Any = None
        # Paradigm the *running* subprocess was spawned with (None = config default).
        self._paradigm: str | None = None
        self._state = ChildProcessState.STOPPED
        # Synchronous API routes may run concurrently in a thread pool.  The
        # re-entrant lock also permits start() to reuse the single stop path
        # when a paradigm change requires a restart.
        self._lifecycle_lock = RLock()

    # ── Public API ─────────────────────────────────────────────────────────

    def start(
        self, paradigm: str | None = None, *, settings_snapshot: dict | None = None,
    ) -> None:
        """Spawn the Stimulation subprocess (no-op if auto_spawn=False or already running).

        A snapshot pins every setting and determines the selected paradigm and
        managed mode. A differing legacy override requires a configuration apply.
        """
        with self._lifecycle_lock:
            snapshot = deepcopy(settings_snapshot)
            if snapshot is not None:
                selected = snapshot.get("STIMULATION.paradigm")
                if paradigm is not None and paradigm != selected:
                    raise RuntimeError("Apply the paradigm configuration before starting Stimulation")
                paradigm = selected
                auto_spawn = bool(snapshot.get("STIMULATION.auto_spawn", self._default_auto_spawn))
            else:
                auto_spawn = self._default_auto_spawn
            if not auto_spawn:
                self._stop_locked()
                self._auto_spawn = False
                logger.debug(
                    "auto_spawn=False — Stimulation process must be started manually"
                )
                return
            self._auto_spawn = True
            self._refresh_state_locked()
            if self._process is not None:
                process_alive = self._process.is_alive()
                if (
                    process_alive
                    and self._state is ChildProcessState.READY
                    and paradigm == self._paradigm
                    and snapshot == self._settings_snapshot
                ):
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
            startup_receiver, startup_sender = mp.Pipe(duplex=False)
            process_kwargs = (
                {"kwargs": {"settings_snapshot": deepcopy(snapshot)}}
                if snapshot is not None else {}
            )
            process = mp.Process(
                target=_run_stimulation,
                args=(paradigm, stop_event, startup_sender),
                name="StimulationProcess",
                daemon=True,
                **process_kwargs,
            )
            self._state = ChildProcessState.STARTING
            try:
                process.start()
            except BaseException:
                self._state = ChildProcessState.FAILED
                for connection in (startup_receiver, startup_sender):
                    try:
                        connection.close()
                    except OSError:
                        pass
                close = getattr(process, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                raise

            self._process = process
            self._stop_event = stop_event
            self._startup_receiver = startup_receiver
            self._paradigm = paradigm
            self._settings_snapshot = snapshot
            try:
                startup_sender.close()
            except OSError as exc:
                logger.warning(
                    "Failed to close parent copy of Stimulation startup sender: %s",
                    exc,
                )
            logger.info(
                "Stimulation subprocess spawned; waiting for readiness "
                "(pid=%s, paradigm=%s)",
                process.pid,
                paradigm or "<config>",
            )
            try:
                expected_identity = ({
                    "expected_revision": snapshot.get("CONFIG.revision"),
                    "expected_generation": snapshot.get("CONFIG.generation"),
                } if snapshot is not None else {})
                wait_for_startup_report(
                    startup_receiver,
                    process,
                    component="Stimulation",
                    timeout_s=self._startup_timeout_s,
                    **expected_identity,
                )
            except BaseException as startup_exc:
                self._state = ChildProcessState.FAILED
                try:
                    self._stop_locked(final_state=ChildProcessState.FAILED)
                except BaseException as cleanup_exc:
                    raise startup_exc from cleanup_exc
                raise
            finally:
                self._close_startup_receiver()

            self._state = ChildProcessState.READY
            logger.info(
                "Stimulation subprocess ready (pid=%s, paradigm=%s)",
                process.pid,
                paradigm or "<config>",
            )

    def stop(self) -> None:
        """Cooperatively stop, escalating without ever losing a live handle."""
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(
        self,
        *,
        final_state: ChildProcessState = ChildProcessState.STOPPED,
    ) -> None:
        process = self._process
        if process is None:
            self._close_startup_receiver()
            self._state = final_state
            return

        self._state = ChildProcessState.STOPPING
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
            self._state = ChildProcessState.FAILED
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
        self._close_startup_receiver()
        self._paradigm = None
        self._settings_snapshot = None
        self._state = final_state

    def _close_startup_receiver(self) -> None:
        receiver = self._startup_receiver
        self._startup_receiver = None
        if receiver is None:
            return
        try:
            receiver.close()
        except OSError:
            pass

    def _refresh_state_locked(self) -> None:
        if (
            self._process is not None
            and self._state in (
                ChildProcessState.STARTING,
                ChildProcessState.READY,
            )
            and not self._process.is_alive()
        ):
            self._state = ChildProcessState.FAILED

    @property
    def auto_spawn(self) -> bool:
        """Whether this manager owns the stimulation subprocess lifecycle."""
        return self._auto_spawn

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return self._process is not None and self._process.is_alive()

    @property
    def is_ready(self) -> bool:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return (
                self._state is ChildProcessState.READY
                and self._process is not None
                and self._process.is_alive()
            )

    @property
    def state(self) -> ChildProcessState:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return self._state

    @property
    def current_paradigm(self) -> str | None:
        """Paradigm the running subprocess was spawned with (None if stopped or config default)."""
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return (
                self._paradigm
                if (
                    self._state is ChildProcessState.READY
                    and self._process is not None
                    and self._process.is_alive()
                )
                else None
            )
