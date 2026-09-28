"""Local READY/FAILED child-process control protocol."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError,
    ChildProcessStartupTimeout,
    ChildProcessState,
    StartupReport,
    StartupReporter,
    wait_for_startup_report,
)


def _process(*, alive: bool = True, exitcode: int | None = None):
    process = MagicMock()
    process.is_alive.return_value = alive
    process.exitcode = exitcode
    return process


def test_reporter_sends_exactly_one_terminal_result_and_closes() -> None:
    connection = MagicMock()
    reporter = StartupReporter(connection, component="Actuation")

    assert reporter.ready() is True
    assert reporter.failed("late", RuntimeError("ignored")) is False

    connection.send.assert_called_once_with(StartupReport.ready("Actuation"))
    connection.close.assert_called_once_with()
    assert reporter.reported is True


def test_reporter_flattens_unpickleable_exception_to_text() -> None:
    connection = MagicMock()
    reporter = StartupReporter(connection, component="Stimulation")

    reporter.failed("renderer", ValueError("no display"))

    report = connection.send.call_args.args[0]
    assert report == StartupReport(
        component="Stimulation",
        state=ChildProcessState.FAILED,
        stage="renderer",
        error_type="ValueError",
        message="no display",
    )


def test_wait_accepts_ready_only_while_child_is_alive() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready("Actuation")
    process = _process(alive=True)

    report = wait_for_startup_report(
        receiver,
        process,
        component="Actuation",
        timeout_s=1.0,
    )

    assert report.state is ChildProcessState.READY


def test_wait_surfaces_child_failure_details() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.failed(
        "Actuation",
        stage="initialization",
        error=RuntimeError("device busy"),
    )

    with pytest.raises(ChildProcessStartupError, match="device busy") as raised:
        wait_for_startup_report(
            receiver,
            _process(),
            component="Actuation",
            timeout_s=1.0,
        )

    assert raised.value.report is receiver.recv.return_value


def test_wait_detects_exit_before_report() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = False
    process = _process(alive=False, exitcode=7)

    with pytest.raises(
        ChildProcessStartupError,
        match=r"exitcode=7",
    ):
        wait_for_startup_report(
            receiver,
            process,
            component="Stimulation",
            timeout_s=1.0,
        )

    process.join.assert_called_once_with(timeout=0)


def test_wait_times_out_boundedly() -> None:
    receiver = MagicMock()
    receiver.poll.return_value = False

    with pytest.raises(ChildProcessStartupTimeout, match="within"):
        wait_for_startup_report(
            receiver,
            _process(alive=True),
            component="Stimulation",
            timeout_s=0.001,
        )


def test_reporter_preserves_configuration_identity() -> None:
    connection = MagicMock()
    reporter = StartupReporter(connection, component="Acquisition",
                               config_revision="revision", generation="run-2")
    reporter.ready()
    report = connection.send.call_args.args[0]
    assert report.config_revision == "revision"
    assert report.generation == "run-2"


@pytest.mark.parametrize("revision,generation", [("old", "run-2"), ("revision", "run-1"), (None, None)])
def test_wait_rejects_stale_or_unidentified_report(revision, generation) -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = StartupReport.ready(
        "Acquisition", config_revision=revision, generation=generation,
    )
    with pytest.raises(ChildProcessStartupError, match="mismatch"):
        wait_for_startup_report(receiver, _process(), component="Acquisition",
                                timeout_s=1, expected_revision="revision",
                                expected_generation="run-2")


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_wait_rejects_invalid_deadlines(timeout) -> None:
    with pytest.raises(ValueError, match="timeout"):
        wait_for_startup_report(MagicMock(), _process(), component="Acquisition",
                                timeout_s=timeout)


@pytest.mark.parametrize("report", [
    "ready", StartupReport.ready("other"),
    StartupReport("Acquisition", ChildProcessState.STARTING, "plugins"),
])
def test_wait_rejects_invalid_acknowledgements(report) -> None:
    receiver = MagicMock()
    receiver.poll.return_value = True
    receiver.recv.return_value = report
    with pytest.raises(ChildProcessStartupError):
        wait_for_startup_report(receiver, _process(), component="Acquisition", timeout_s=1)
