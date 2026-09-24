"""External stimulation clears its real trigger only after rendering stops."""

from unittest.mock import AsyncMock, Mock

import pytest

from omnibci.infrastructure.stimulation import application as module
from omnibci.infrastructure.stimulation.render_loop import RenderLoop


def make_app(monkeypatch, *, external=True, render_error=False, clear_error=False,
             setup_error=False, initialize_trigger=True):
    monkeypatch.setattr(module.mp, "Queue", Mock())
    app = module.Stimulation(settings_snapshot={
        "CONFIG.node_role": "stimulation" if external else None,
        "CONFIG.revision": "a" * 64,
        "CONFIG.generation": "run-1",
    })
    steps = []

    class Trigger:
        def clear(self):
            assert app._render_task.done()
            steps.append("trigger-clear")
            if clear_error:
                raise RuntimeError("trigger release unconfirmed")

    class Renderer:
        def should_close(self):
            return False
        def poll_events(self):
            pass
        def end_frame(self):
            pass
        def shutdown(self):
            steps.append("renderer-shutdown")

    class Paradigm:
        def render_frame(self, time):
            pass
        def teardown(self, renderer):
            steps.append("paradigm-teardown")
            if render_error:
                raise RuntimeError("renderer release unconfirmed")

    def setup():
        if initialize_trigger:
            app.stimulator.trigger = Trigger()
        if setup_error:
            raise RuntimeError("renderer initialization failed")
        app.stimulator._loop = RenderLoop(Renderer(), Paradigm())

    async def ready():
        if external:
            app._connection_lost()
        else:
            app.should_stop = True

    monkeypatch.setattr(app.stimulator, "_setup", setup)
    monkeypatch.setattr(app, "start", AsyncMock())
    monkeypatch.setattr(app, "_await_managed_readiness", ready)
    monkeypatch.setattr(app, "stop", AsyncMock(side_effect=lambda: steps.append("ipc-stop")))
    return app, steps


def test_reconnect_requires_stopped_renderer_then_cleared_trigger(monkeypatch):
    app, steps = make_app(monkeypatch)
    assert app.run() is True
    assert steps == ["paradigm-teardown", "renderer-shutdown", "trigger-clear", "ipc-stop"]


def test_trigger_clear_failure_is_fatal_and_still_closes_ipc(monkeypatch):
    app, steps = make_app(monkeypatch, clear_error=True)
    with pytest.raises(RuntimeError, match="trigger release unconfirmed"):
        app.run()
    assert steps == ["paradigm-teardown", "renderer-shutdown", "trigger-clear", "ipc-stop"]


def test_render_teardown_failure_still_clears_trigger_and_remains_fatal(monkeypatch):
    app, steps = make_app(monkeypatch, render_error=True)
    with pytest.raises(RuntimeError, match="renderer release unconfirmed"):
        app.run()
    assert steps == ["paradigm-teardown", "trigger-clear", "ipc-stop"]


@pytest.mark.parametrize("initialize_trigger", [False, True])
def test_failed_setup_clears_only_an_initialized_trigger(monkeypatch, initialize_trigger):
    app, steps = make_app(monkeypatch, setup_error=True, initialize_trigger=initialize_trigger)
    with pytest.raises(RuntimeError, match="renderer initialization failed"):
        app.run()
    assert steps == (["trigger-clear"] if initialize_trigger else []) + ["ipc-stop"]


def test_managed_shutdown_preserves_existing_trigger_lifecycle(monkeypatch):
    app, steps = make_app(monkeypatch, external=False)
    assert app.run() is False
    assert steps == ["paradigm-teardown", "renderer-shutdown", "ipc-stop"]
