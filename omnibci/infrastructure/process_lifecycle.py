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

from dataclasses import dataclass
from enum import StrEnum
import logging
import math
import time
from typing import Any

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.05


class ChildProcessState(StrEnum):
    """Lifecycle state exposed by managed child processes."""

    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class StartupReport:
    """One terminal startup result sent from a child to its parent."""

    component: str
    state: ChildProcessState
    stage: str
    error_type: str | None = None
    message: str | None = None
    config_revision: str | None = None
    generation: str | None = None

    @classmethod
    def ready(cls, component: str, *, config_revision: str | None = None,
              generation: str | None = None) -> "StartupReport":
        return cls(
            component=component,
            state=ChildProcessState.READY,
            stage="ready",
            config_revision=config_revision,
            generation=generation,
        )

    @classmethod
    def failed(
        cls,
        component: str,
        *,
        stage: str,
        error: BaseException,
        config_revision: str | None = None,
        generation: str | None = None,
    ) -> "StartupReport":
        return cls(
            component=component,
            state=ChildProcessState.FAILED,
            stage=stage,
            error_type=type(error).__name__,
            message=str(error),
            config_revision=config_revision,
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
                 config_revision: str | None = None, generation: str | None = None) -> None:
        self._connection = connection
        self._component = component
        self._reported = False
        self._config_revision = config_revision
        self._generation = generation

    @property
    def reported(self) -> bool:
        return self._reported

    def ready(self) -> bool:
        return self._send(StartupReport.ready(
            self._component, config_revision=self._config_revision, generation=self._generation,
        ))

    def failed(self, stage: str, error: BaseException) -> bool:
        return self._send(
            StartupReport.failed(
                self._component,
                stage=stage,
                error=error,
                config_revision=self._config_revision,
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


def wait_for_startup_report(
    receiver: Any,
    process: Any,
    *,
    component: str,
    timeout_s: float,
    expected_revision: str | None = None,
    expected_generation: str | None = None,
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
                ("configuration revision", expected_revision, report.config_revision),
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
