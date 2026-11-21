"""Local and external stimulation nodes share the same resource cleanup owner."""
from unittest.mock import AsyncMock

import pytest

from tests.execution_helpers import synthetic_execution_snapshot

from omnibci.infrastructure.stimulation.application import Stimulation


def make_app(monkeypatch, *, clear_error=False, render_error=False, setup_error=False,
             initialize_trigger=True, external=True):
    snapshot = {"CONFIG.node_role": "stimulation", "CONFIG.module_revisions": {"stimulation": "1" * 64},
                "CONFIG.engine_revision": "2" * 64, "CONFIG.generation": "generation"} if external else {}
    if external:
        snapshot = synthetic_execution_snapshot(snapshot)
    app = Stimulation(settings_snapshot=snapshot)
    steps = []

    class Trigger:
        def setup(self):
            if not initialize_trigger:
                raise RuntimeError('trigger initialization failed')
        def clear(self):
            steps.append('trigger-clear')
            if clear_error:
                raise RuntimeError('trigger release unconfirmed')
        def teardown(self):
            steps.append('trigger-teardown')

    class Renderer:
        def initialize(self, **kwargs):
            pass
        def should_close(self):
            return False
        def poll_events(self):
            pass
        def begin_frame(self):
            return None
        def end_frame(self, frame, *, submit):
            pass
        def shutdown(self):
            steps.append('renderer-shutdown')

    class Paradigm:
        def setup(self, *, renderer, trigger):
            if setup_error:
                raise RuntimeError('paradigm initialization failed')
        def render_frame(self, frame):
            return False
        def teardown(self):
            steps.append('paradigm-teardown')
            if render_error:
                raise RuntimeError('renderer release unconfirmed')

    async def ready():
        if external:
            app._connection_lost()
        else:
            app.should_stop = True

    monkeypatch.setattr(app.stimulator, '_load_trigger', lambda: Trigger())
    monkeypatch.setattr(app.stimulator, '_load_paradigm', lambda: Paradigm())
    monkeypatch.setattr('omnibci.infrastructure.stimulation.stimulator._create_renderer', lambda name: Renderer())
    monkeypatch.setattr(app, 'start', AsyncMock())
    monkeypatch.setattr(app, '_await_managed_readiness', ready)
    async def close():
        steps.append('ipc-stop')
        app._close_event_queue()
    monkeypatch.setattr(app, 'stop', close)
    return app, steps


def test_reconnect_requires_stopped_renderer_and_cleared_trigger(monkeypatch):
    app, steps = make_app(monkeypatch)
    assert app.run() is True
    assert steps == ['trigger-clear', 'paradigm-teardown', 'trigger-clear', 'trigger-teardown', 'renderer-shutdown', 'ipc-stop']


def test_trigger_clear_failure_is_fatal_and_still_closes_ipc(monkeypatch):
    app, steps = make_app(monkeypatch, clear_error=True)
    with pytest.raises(ExceptionGroup, match='cleanup also failed'):
        app.run()
    assert steps == ['trigger-clear', 'trigger-clear', 'trigger-teardown', 'renderer-shutdown', 'ipc-stop']


def test_render_teardown_failure_still_clears_trigger_and_closes_renderer(monkeypatch):
    app, steps = make_app(monkeypatch, render_error=True)
    with pytest.raises(RuntimeError, match='renderer release unconfirmed'):
        app.run()
    assert steps == ['trigger-clear', 'paradigm-teardown', 'trigger-clear', 'trigger-teardown', 'renderer-shutdown', 'ipc-stop']


@pytest.mark.parametrize('initialize_trigger', [False, True])
def test_failed_setup_clears_only_a_successfully_initialized_trigger(monkeypatch, initialize_trigger):
    app, steps = make_app(monkeypatch, setup_error=True, initialize_trigger=initialize_trigger)
    with pytest.raises(RuntimeError, match='initialization failed'):
        app.run()
    assert steps == (['trigger-clear', 'paradigm-teardown', 'trigger-clear'] if initialize_trigger else []) + [
        'trigger-teardown', 'renderer-shutdown', 'ipc-stop']


def test_managed_shutdown_uses_same_trigger_lifecycle(monkeypatch):
    app, steps = make_app(monkeypatch, external=False)
    assert app.run() is False
    assert steps == ['trigger-clear', 'paradigm-teardown', 'trigger-clear', 'trigger-teardown', 'renderer-shutdown', 'ipc-stop']
