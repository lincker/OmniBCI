"""Local child-process startup reporting.

This control plane is deliberately separate from the engine/peer wire
protocol.  It exists only between a process manager and the child process it
spawned on the same machine, and answers one narrow question: did the child
become usable, or did it fail while initialising?

Only primitive error details cross the pipe.  Exception objects and tracebacks
are intentionally kept in the child process because arbitrary plugin
exceptions are not guaranteed to be pickleable.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
import logging
import math
import sys
from threading import RLock
import time
from typing import Any, Callable

from omnibci.application.configuration.definitions import (
    MODULE_SETTING_DEFAULTS,
    module_runtime_settings,
)
from omnibci.application.plugin_management.execution import PluginExecutionReceipt
from omnibci.infrastructure.plugin_management.preparation import execution_receipt

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.05
CLEAN_FAILURE_EXITCODE = 65  # Startup/runtime error after confirmed child teardown.


class ChildProcessState(StrEnum):
    """Lifecycle state exposed by managed child processes."""

    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


def module_runtime_identity(snapshot: dict | None, module: str) -> tuple:
    """Only owned configuration and its runtime determine child reuse."""
    snapshot = snapshot or {}
    return (snapshot.get("CONFIG.module_revisions", {}).get(module),
            snapshot.get("CONFIG.engine_revision"), snapshot.get("CONFIG.generation"))


@dataclass(frozen=True, slots=True)
class StartupReport:
    """One terminal startup result sent from a child to its parent."""

    component: str
    state: ChildProcessState
    stage: str
    error_type: str | None = None
    message: str | None = None
    module_revisions: dict[str, str] | None = None
    engine_revision: str | None = None
    generation: str | None = None
    execution: PluginExecutionReceipt | None = None

    @classmethod
    def ready(cls, component: str, *, module_revisions: dict[str, str] | None = None,
              engine_revision: str | None = None,
              generation: str | None = None,
              execution: PluginExecutionReceipt | None = None) -> "StartupReport":
        return cls(
            component=component,
            state=ChildProcessState.READY,
            stage="ready",
            module_revisions=dict(module_revisions) if module_revisions is not None else None,
            engine_revision=engine_revision,
            generation=generation,
            execution=execution,
        )

    @classmethod
    def failed(
        cls,
        component: str,
        *,
        stage: str,
        error: BaseException,
        module_revisions: dict[str, str] | None = None,
        engine_revision: str | None = None,
        generation: str | None = None,
    ) -> "StartupReport":
        return cls(
            component=component,
            state=ChildProcessState.FAILED,
            stage=stage,
            error_type=type(error).__name__,
            message=str(error),
            module_revisions=dict(module_revisions) if module_revisions is not None else None,
            engine_revision=engine_revision,
            generation=generation,
        )


class ChildProcessStartupError(RuntimeError):
    """A managed child exited or explicitly reported failed startup."""

    def __init__(self, message: str, *, report: StartupReport | None = None) -> None:
        super().__init__(message)
        self.report = report

    @classmethod
    def from_report(cls, report: StartupReport) -> "ChildProcessStartupError":
        detail = report.error_type or "startup error"
        if report.message:
            detail = f"{detail}: {report.message}"
        return cls(
            f"{report.component} subprocess failed during {report.stage}: {detail}",
            report=report,
        )


class ChildProcessStartupTimeout(TimeoutError):
    """A managed child did not report READY/FAILED within its deadline."""


class StartupReporter:
    """Child-side exactly-once sender for a startup result."""

    def __init__(self, connection: Any, *, component: str,
                 module_revisions: dict[str, str] | None = None,
                 engine_revision: str | None = None, generation: str | None = None) -> None:
        self._connection = connection
        self._component = component
        self._reported = False
        self._module_revisions = dict(module_revisions) if module_revisions is not None else None
        self._engine_revision = engine_revision
        self._generation = generation

    @property
    def reported(self) -> bool:
        return self._reported

    def ready(self, *, execution: PluginExecutionReceipt | None = None) -> bool:
        return self._send(StartupReport.ready(
            self._component, module_revisions=self._module_revisions,
            engine_revision=self._engine_revision, generation=self._generation,
            execution=execution,
        ))

    def failed(self, stage: str, error: BaseException) -> bool:
        return self._send(
            StartupReport.failed(
                self._component,
                stage=stage,
                error=error,
                module_revisions=self._module_revisions, engine_revision=self._engine_revision,
                generation=self._generation,
            )
        )

    def _send(self, report: StartupReport) -> bool:
        if self._reported or self._connection is None:
            return False
        self._reported = True
        connection = self._connection
        self._connection = None
        try:
            connection.send(report)
            return True
        except (BrokenPipeError, EOFError, OSError) as exc:
            # The parent may have timed out and closed its receive end.  Startup
            # cleanup in the child must continue even when reporting is no
            # longer possible.
            logger.warning(
                "%s subprocess could not report %s startup: %s",
                self._component,
                report.state,
                exc,
            )
            return False
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class StartupChannel:
    """Parent-owned handles for one worker's dedicated startup pipe."""

    component: str
    process: Any
    receiver: Any
    reporter: StartupReporter
    expected_execution: PluginExecutionReceipt | None = None


def wait_for_startup_report(
    receiver: Any,
    process: Any,
    *,
    component: str,
    timeout_s: float,
    expected_module_revisions: dict[str, str] | None = None,
    expected_engine_revision: str | None = None,
    expected_generation: str | None = None,
    expected_execution: PluginExecutionReceipt | None = None,
) -> StartupReport:
    """Wait boundedly for one valid READY/FAILED report.

    Process liveness is sampled while waiting so an early child exit is
    reported immediately instead of being mislabelled as a timeout.
    """
    if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s) or timeout_s <= 0):
        raise ValueError("startup timeout must be finite and greater than zero")

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ChildProcessStartupTimeout(
                f"{component} subprocess did not become ready within "
                f"{timeout_s:g}s"
            )

        try:
            has_report = receiver.poll(min(_POLL_INTERVAL_S, remaining))
        except (EOFError, OSError) as exc:
            raise ChildProcessStartupError(
                f"{component} startup control channel failed: {exc}"
            ) from exc

        if has_report:
            try:
                report = receiver.recv()
            except (EOFError, OSError) as exc:
                raise ChildProcessStartupError(
                    f"{component} subprocess exited before reporting startup"
                ) from exc
            if not isinstance(report, StartupReport):
                raise ChildProcessStartupError(
                    f"{component} subprocess sent an invalid startup report"
                )
            if report.component != component:
                raise ChildProcessStartupError(
                    f"{component} subprocess sent a report for "
                    f"{report.component!r}"
                )
            for label, expected, actual in (
                ("module revisions", expected_module_revisions, report.module_revisions),
                ("Engine revision", expected_engine_revision, report.engine_revision),
                ("generation", expected_generation, report.generation),
            ):
                if expected is not None and actual != expected:
                    raise ChildProcessStartupError(
                        f"{component} startup {label} mismatch: expected {expected!r}, "
                        f"received {actual!r}", report=report,
                    )
            if report.state is ChildProcessState.FAILED:
                raise ChildProcessStartupError.from_report(report)
            if report.state is not ChildProcessState.READY:
                raise ChildProcessStartupError(
                    f"{component} subprocess sent non-terminal startup state "
                    f"{report.state}"
                )
            if report.execution != expected_execution:
                raise ChildProcessStartupError(f"{component} startup execution evidence mismatch", report=report)
            if not process.is_alive():
                process.join(timeout=0)
                raise ChildProcessStartupError(
                    f"{component} subprocess exited immediately after reporting "
                    f"ready (exitcode={process.exitcode})",
                    report=report,
                )
            return report

        if not process.is_alive():
            process.join(timeout=0)
            raise ChildProcessStartupError(
                f"{component} subprocess exited before reporting ready "
                f"(exitcode={process.exitcode})"
            )


def run_managed_child(
    app_factory: Callable[..., Any],
    *,
    component: str,
    module: str,
    stop_event: Any = None,
    startup_connection: Any = None,
    settings_snapshot: dict,
) -> None:
    """Run a spawned output-host application with structured startup reporting."""
    reporter = (
        StartupReporter(
            startup_connection,
            component=component,
            module_revisions={module: settings_snapshot.get("CONFIG.module_revisions", {}).get(module)},
            engine_revision=settings_snapshot.get("CONFIG.engine_revision"),
            generation=settings_snapshot.get("CONFIG.generation"),
        )
        if startup_connection is not None
        else None
    )
    app = None
    try:
        kwargs = {"stop_event": stop_event, "settings_snapshot": settings_snapshot}
        if reporter is not None:
            kwargs["startup_reporter"] = reporter
        app = app_factory(**kwargs)
        app.run()
    except BaseException as exc:
        if reporter is not None:
            reporter.failed("entrypoint", exc)
        if getattr(app, "cleanup_confirmed", False) is True:
            logger.exception("%s subprocess failed with confirmed cleanup", component)
            raise SystemExit(CLEAN_FAILURE_EXITCODE) from exc
        raise
    finally:
        if reporter is not None:
            reporter.close()


class ManagedOutputProcessManager:
    """Shared OS-process lifecycle for managed output hosts (Stimulation, Actuation).

    Each start uses the pinned snapshot's managed or external mode. Desktop
    hosts disable automatic termination and retain unresponsive children so an
    operator can confirm device settlement explicitly.
    """

    _COMPONENT_NAME: str
    _MODULE_NAME: str
    _AUTO_SPAWN_KEY: str
    _PROCESS_NAME: str
    _SELECTION_LABEL: str
    _RESOURCE_NOUN: str

    _GRACEFUL_TIMEOUT_S = 3.0
    _TERMINATE_TIMEOUT_S = 3.0
    _KILL_TIMEOUT_S = 3.0
    _STARTUP_TIMEOUT_S = 10.0

    def __init__(
        self,
        *,
        startup_timeout_s: float | None = None,
        allow_process_termination: bool = True,
        record_execution=None,
    ) -> None:
        timeout = self._STARTUP_TIMEOUT_S if startup_timeout_s is None else startup_timeout_s
        if timeout <= 0:
            raise ValueError("startup_timeout_s must be greater than zero")
        self._record_execution = record_execution
        self._auto_spawn = True
        self._settings_snapshot: dict | None = None
        self.execution_receipt = None
        self._startup_timeout_s = float(timeout)
        self._allow_process_termination = allow_process_termination
        self._process: Any = None
        self._stop_event: Any = None
        self._startup_receiver: Any = None
        self._selection: str | None = None
        self._state = ChildProcessState.STOPPED
        self._cleanup_error: str | None = None
        # Synchronous API routes may run concurrently in a thread pool. The
        # re-entrant lock also permits start() to reuse the single stop path
        # when a plugin change requires a restart.
        self._lifecycle_lock = RLock()

    def _configured_selection(self, snapshot: dict) -> str | None:
        raise NotImplementedError

    def _process_target(self) -> Callable[..., None]:
        raise NotImplementedError

    def _runtime_module(self):
        return sys.modules[type(self).__module__]

    def start(self, *, settings_snapshot: dict) -> None:
        """Start a managed child or release it when external mode is selected."""
        with self._lifecycle_lock:
            self._start_locked(settings_snapshot)

    def _start_locked(self, settings_snapshot: dict) -> None:
        if self._process is None and self._cleanup_error is not None:
            raise RuntimeError(self._cleanup_error)
        snapshot = deepcopy(settings_snapshot)
        auto_spawn = snapshot.get(
            self._AUTO_SPAWN_KEY,
            MODULE_SETTING_DEFAULTS[self._MODULE_NAME][self._AUTO_SPAWN_KEY],
        )
        if not auto_spawn:
            self._stop_locked()
            self._auto_spawn = False
            logger.debug(
                "auto_spawn=False — %s process must be started manually",
                self._COMPONENT_NAME,
            )
            return
        selection = self._configured_selection(snapshot)
        self._auto_spawn = True
        self._refresh_state_locked()
        if self._process is not None:
            process_alive = self._process.is_alive()
            if (
                process_alive
                and self._state is ChildProcessState.READY
                and selection == self._selection
                and module_runtime_identity(snapshot, self._MODULE_NAME)
                == module_runtime_identity(self._settings_snapshot, self._MODULE_NAME)
            ):
                logger.debug(
                    "%s process already running requested %s (pid=%s, %s=%s)",
                    self._COMPONENT_NAME,
                    self._SELECTION_LABEL,
                    self._process.pid,
                    self._SELECTION_LABEL,
                    selection or "<config>",
                )
                return
            if process_alive:
                logger.info(
                    "%s %s change %s → %s; restarting subprocess",
                    self._COMPONENT_NAME,
                    self._SELECTION_LABEL,
                    self._selection or "<config>",
                    selection or "<config>",
                )
            else:
                logger.info(
                    "Reaping exited %s subprocess before restart (pid=%s, exitcode=%s)",
                    self._COMPONENT_NAME,
                    self._process.pid,
                    self._process.exitcode,
                )
            # stop() only clears the old handle after confirming exit. If it
            # cannot stop the process it raises and this method must not spawn a
            # duplicate output host.
            self._stop_locked()

        runtime_module = self._runtime_module()
        mp_ctx = runtime_module.mp
        if "CONFIG.operation_id" not in snapshot:
            snapshot["CONFIG.operation_id"] = runtime_module.uuid4().hex
        expected_execution = execution_receipt(
            snapshot, self._MODULE_NAME, snapshot["CONFIG.operation_id"],
        )
        stop_event = mp_ctx.Event()
        startup_receiver, startup_sender = mp_ctx.Pipe(duplex=False)
        process = mp_ctx.Process(
            target=self._process_target(),
            args=(stop_event, startup_sender),
            kwargs={"settings_snapshot": module_runtime_settings(snapshot, self._MODULE_NAME)},
            name=self._PROCESS_NAME,
            daemon=self._allow_process_termination,
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
        self._selection = selection
        self._settings_snapshot = snapshot
        # The parent must not retain the write end; otherwise a child that exits
        # before sending a report can leave the receive end looking live.
        try:
            startup_sender.close()
        except OSError as exc:
            logger.warning(
                "Failed to close parent copy of %s startup sender: %s",
                self._COMPONENT_NAME,
                exc,
            )
        logger.info(
            "%s subprocess spawned; waiting for readiness (pid=%s, %s=%s)",
            self._COMPONENT_NAME,
            process.pid,
            self._SELECTION_LABEL,
            selection or "<config>",
        )
        try:
            report = wait_for_startup_report(
                startup_receiver,
                process,
                component=self._COMPONENT_NAME,
                timeout_s=self._startup_timeout_s,
                expected_module_revisions={
                    self._MODULE_NAME: snapshot.get("CONFIG.module_revisions", {}).get(self._MODULE_NAME),
                },
                expected_engine_revision=snapshot.get("CONFIG.engine_revision"),
                expected_generation=snapshot.get("CONFIG.generation"),
                expected_execution=expected_execution,
            )
            if self._record_execution is not None:
                self._record_execution(report.execution)
        except BaseException as startup_exc:
            self._state = ChildProcessState.FAILED
            try:
                self._stop_locked(final_state=ChildProcessState.FAILED)
            except BaseException as cleanup_exc:
                raise startup_exc from cleanup_exc
            raise
        finally:
            self._close_startup_receiver()

        self.execution_receipt = report.execution
        self._state = ChildProcessState.READY
        logger.info(
            "%s subprocess ready (pid=%s, %s=%s)",
            self._COMPONENT_NAME,
            process.pid,
            self._SELECTION_LABEL,
            selection or "<config>",
        )

    def stop(self) -> None:
        """Apply the host stop policy without ever losing a live handle."""
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
            if self._cleanup_error is not None:
                raise RuntimeError(self._cleanup_error)
            self._state = final_state
            return

        self._state = ChildProcessState.STOPPING
        pid = process.pid
        if process.is_alive():
            if self._stop_event is not None:
                self._stop_event.set()
            process.join(timeout=self._GRACEFUL_TIMEOUT_S)

        if process.is_alive():
            if not self._allow_process_termination:
                self._state = ChildProcessState.FAILED
                raise RuntimeError(
                    f"{self._COMPONENT_NAME} subprocess did not stop gracefully (pid={pid}); "
                    "process retained for an explicit force-exit decision"
                )
            logger.warning(
                "%s subprocess did not stop gracefully; terminating (pid=%s)",
                self._COMPONENT_NAME,
                pid,
            )
            self._cleanup_error = (
                f"{self._COMPONENT_NAME} cleanup was not confirmed after forced termination "
                f"(pid={pid}); replacement is blocked"
            )
            process.terminate()
            process.join(timeout=self._TERMINATE_TIMEOUT_S)

        if process.is_alive():
            kill = getattr(process, "kill", None)
            if callable(kill):
                logger.warning(
                    "%s subprocess ignored terminate; killing (pid=%s)",
                    self._COMPONENT_NAME,
                    pid,
                )
                kill()
                process.join(timeout=self._KILL_TIMEOUT_S)

        if process.is_alive():
            message = (
                f"{self._COMPONENT_NAME} subprocess is still alive after graceful stop, "
                f"terminate, and kill escalation (pid={pid}); refusing to "
                f"discard its handle or start another {self._RESOURCE_NOUN}"
            )
            logger.error(message)
            self._state = ChildProcessState.FAILED
            raise RuntimeError(message)

        exitcode = process.exitcode
        if exitcode not in (0, CLEAN_FAILURE_EXITCODE):
            self._cleanup_error = (
                f"{self._COMPONENT_NAME} cleanup was not confirmed "
                f"(pid={pid}, exitcode={exitcode}); replacement is blocked"
            )
        logger.info(
            "%s subprocess stopped (pid=%s, exitcode=%s)",
            self._COMPONENT_NAME,
            pid,
            exitcode,
        )
        close = getattr(process, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # pragma: no cover - platform defensive
                logger.warning(
                    "Failed to close stopped %s process handle (pid=%s): %s",
                    self._COMPONENT_NAME,
                    pid,
                    exc,
                )
        self._process = None
        self._stop_event = None
        self._close_startup_receiver()
        if self._cleanup_error is not None:
            self._state = ChildProcessState.FAILED
            raise RuntimeError(self._cleanup_error)
        self._selection = None
        self.execution_receipt = None
        self._settings_snapshot = None
        self._state = (
            ChildProcessState.FAILED
            if exitcode == CLEAN_FAILURE_EXITCODE
            else final_state
        )

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

    def _current_selection(self) -> str | None:
        with self._lifecycle_lock:
            self._refresh_state_locked()
            return (
                self._selection
                if (
                    self._state is ChildProcessState.READY
                    and self._process is not None
                    and self._process.is_alive()
                )
                else None
            )

    @property
    def auto_spawn(self) -> bool:
        """Whether the selected configuration requests a managed subprocess."""
        with self._lifecycle_lock:
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
