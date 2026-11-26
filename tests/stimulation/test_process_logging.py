"""Managed Stimulation diagnostics survive a fresh spawn without a display."""

import asyncio
from contextlib import redirect_stderr
import logging
import multiprocessing
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _run_logging_probe(log_path, existing_level):
    from omnibci.infrastructure.stimulation import render_loop
    from omnibci.infrastructure.stimulation.process_manager import _run_stimulation

    def render():
        renderer, paradigm = MagicMock(), MagicMock()
        paradigm.required_refresh_rate_hz = 60.0
        renderer.refresh_rate_hz.return_value = 60.0
        frame_count = 1831
        renderer.should_close.side_effect = [False] * frame_count + [True]
        render_loop.validate_display_refresh_rate(renderer, paradigm)
        samples = iter([0.0, *(i / 59.94 for i in range(frame_count))])
        with patch.object(render_loop.time, "perf_counter", side_effect=lambda: next(samples)):
            asyncio.run(render_loop.RenderLoop(renderer, paradigm).run_async())
        render_loop.logger.warning("Stimulation logging probe completed")

    with open(log_path, "w", encoding="utf-8") as output, redirect_stderr(output):
        # Configure only the preservation case. The default case must rely
        # entirely on the production entry point, not pytest's log capture.
        if existing_level is not None:
            logging.basicConfig(level=existing_level)
        with patch(
            "omnibci.infrastructure.stimulation.application.Stimulation",
            return_value=SimpleNamespace(run=render),
        ):
            _run_stimulation(settings_snapshot={})


@pytest.mark.integration
@pytest.mark.parametrize("existing_level", [None, logging.WARNING], ids=["default", "configured-warning"])
def test_spawned_refresh_rate_logs_reach_stderr(tmp_path, monkeypatch, existing_level):
    # The parent being configured is insufficient: spawn starts a new interpreter.
    monkeypatch.setattr(logging.getLogger("omnibci"), "level", logging.INFO)
    log_path = tmp_path / "stimulation-stderr.log"
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_run_logging_probe, args=(str(log_path), existing_level))
    try:
        process.start()
        process.join(timeout=20)
        assert not process.is_alive(), "Stimulation logging probe did not finish"
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()

    output = log_path.read_text(encoding="utf-8")
    assert output.count("Stimulation logging probe completed") == 1
    expected = (
        "Display refresh rate verified: system=60 Hz, paradigm_required=60 Hz",
        "RenderLoop measured refresh rate: 59.94 Hz",
        "RenderLoop session measured refresh rate: 59.94 Hz",
    )
    for message in expected:
        assert output.count(message) == (1 if existing_level is None else 0), output
