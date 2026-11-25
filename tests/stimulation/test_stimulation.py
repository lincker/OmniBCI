"""
Unit tests for Stimulator.

All external dependencies are mocked:
  - _create_renderer   : returns a MagicMock renderer
  - _load_trigger      : returns a MagicMock trigger
  - _load_paradigm     : returns a MagicMock paradigm
  - RenderLoop         : patched so no graphics operations run
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch, call
from types import SimpleNamespace

import asyncio
import numpy as np
import pytest

from omnibci.infrastructure.stimulation.stimulator import Stimulator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stimulator():
    """Return a Stimulator with all IO mocked, ready to call start_async()."""
    s = Stimulator(settings_snapshot={"STIMULATION.renderer": "moderngl"})
    mock_renderer  = MagicMock()
    mock_paradigm  = MagicMock()
    mock_trigger   = MagicMock()
    mock_loop      = MagicMock()
    mock_loop.run_async = AsyncMock()
    for obj, methods in ((mock_renderer, ("initialize", "shutdown")),
                         (mock_trigger, ("setup", "clear", "teardown")),
                         (mock_paradigm, ("setup", "teardown"))):
        for method in methods:
            getattr(obj, method).return_value = None

    s._load_trigger  = MagicMock(return_value=mock_trigger)
    s._load_paradigm = MagicMock(return_value=mock_paradigm)

    return s, mock_renderer, mock_paradigm, mock_trigger, mock_loop


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStimulatorStart:
    def test_creates_renderer_from_config(self):
        s, mock_renderer, _, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ) as mock_create, patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        # config says "moderngl"
        mock_create.assert_called_once_with("moderngl")

    def test_renderer_initialize_called(self):
        s, mock_renderer, _, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        mock_renderer.initialize.assert_called_once_with(
            width=1920, height=1080, title="OmniBCI Stimulation"
        )

    def test_paradigm_setup_receives_both_capabilities(self):
        s, mock_renderer, mock_paradigm, mock_trigger, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        mock_paradigm.setup.assert_called_once_with(renderer=mock_renderer, trigger=mock_trigger)

    def test_render_loop_started(self):
        s, mock_renderer, mock_paradigm, _, mock_loop = _make_stimulator()
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ) as MockLoop:
            asyncio.run(s.start_async())
        MockLoop.assert_called_once_with(mock_renderer, mock_paradigm)
        mock_loop.run_async.assert_awaited_once()

    def test_trigger_loaded_before_paradigm(self):
        s, mock_renderer, mock_paradigm, mock_trigger, mock_loop = _make_stimulator()
        call_order = []
        s._load_trigger  = MagicMock(side_effect=lambda: call_order.append("trigger") or mock_trigger)
        s._load_paradigm = MagicMock(side_effect=lambda: call_order.append("paradigm") or mock_paradigm)
        with patch(
            "omnibci.infrastructure.stimulation.stimulator._create_renderer",
            return_value=mock_renderer,
        ), patch(
            "omnibci.infrastructure.stimulation.stimulator.RenderLoop",
            return_value=mock_loop,
        ):
            asyncio.run(s.start_async())
        assert call_order == ["trigger", "paradigm"]


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStimulatorStop:
    def test_request_stop_signals_without_discarding_loop(self):
        s = Stimulator(settings_snapshot={})
        loop = MagicMock()
        s._loop = loop
        s.request_stop()
        loop.request_stop.assert_called_once_with()
        assert s._loop is loop

    def test_request_stop_without_start_is_noop(self):
        Stimulator(settings_snapshot={}).request_stop()


# ---------------------------------------------------------------------------
# present_trial / present_result
# ---------------------------------------------------------------------------

class TestStimulatorPresent:
    def _started(self):
        s = Stimulator(settings_snapshot={})
        s.paradigm = MagicMock()
        s.paradigm.present_trial.return_value = None
        s.paradigm.present_result.return_value = None
        s._payload_schemas = {name: {"type": "object", "properties": {key: {"type": "integer"}},
                                    "required": [key], "additionalProperties": False}
                              for name, key in (("trial", "event_id"), ("result", "target_id"))}
        return s, s.paradigm

    def test_present_trial_delegates(self):
        s, mock_paradigm = self._started()
        s.present_trial({"event_id": 5})
        mock_paradigm.present_trial.assert_called_once_with({"event_id": 5})

    def test_present_result_delegates(self):
        s, mock_paradigm = self._started()
        s.present_result({"target_id": 3})
        mock_paradigm.present_result.assert_called_once_with({"target_id": 3})

    def test_present_trial_requires_paradigm(self):
        s = Stimulator(settings_snapshot={})
        with pytest.raises(RuntimeError, match="not been initialized"):
            s.present_trial(0)

    def test_present_result_requires_paradigm(self):
        s = Stimulator(settings_snapshot={})
        with pytest.raises(RuntimeError, match="not been initialized"):
            s.present_result(0)


class TestStimulatorReadiness:
    @pytest.mark.parametrize("presenting", [False, True])
    def test_returns_required_boolean_without_coercion(self, presenting):
        s = Stimulator(settings_snapshot={})
        s.paradigm = MagicMock()
        s.paradigm.is_presenting.return_value = presenting

        assert s.is_presenting is presenting
        s.paradigm.is_presenting.assert_called_once_with()

    def test_uninitialized_paradigm_is_not_ready(self):
        s = Stimulator(settings_snapshot={})
        with pytest.raises(RuntimeError, match="not been initialized"):
            _ = s.is_presenting

    def test_missing_readiness_method_is_not_ready(self):
        s = Stimulator(settings_snapshot={})
        s.paradigm = SimpleNamespace()
        with pytest.raises(AttributeError, match="is_presenting"):
            _ = s.is_presenting

    @pytest.mark.parametrize("presenting", [
        None, 0, 1, "", "busy", [], object(), np.bool_(False), np.bool_(True),
    ])
    def test_rejects_non_boolean_readiness(self, presenting):
        s = Stimulator(settings_snapshot={})
        s.paradigm = MagicMock()
        s.paradigm.is_presenting.return_value = presenting

        with pytest.raises(TypeError, match="is_presenting.*bool"):
            _ = s.is_presenting

    def test_propagates_readiness_error(self):
        s = Stimulator(settings_snapshot={})
        error = RuntimeError("readiness failed")
        s.paradigm = MagicMock()
        s.paradigm.is_presenting.side_effect = error

        with pytest.raises(RuntimeError) as raised:
            _ = s.is_presenting
        assert raised.value is error


# ---------------------------------------------------------------------------
# _create_renderer
# ---------------------------------------------------------------------------

class TestCreateRenderer:
    def test_unknown_renderer_raises(self):
        from omnibci.infrastructure.stimulation.stimulator import _create_renderer
        with pytest.raises(ValueError, match="Unknown renderer"):
            _create_renderer("vulkan")

    def test_known_renderer_returns_instance(self):
        from omnibci.infrastructure.stimulation.stimulator import _create_renderer
        FakeCls = MagicMock(return_value=MagicMock())
        with patch.dict(
            "omnibci.infrastructure.stimulation.stimulator._RENDERER_REGISTRY",
            {"fake": "some.module.FakeCls"},
        ), patch("importlib.import_module") as mock_import:
            mock_import.return_value = MagicMock(FakeCls=FakeCls)
            _create_renderer("fake")
            mock_import.assert_called_once_with("some.module")


import threading
from unittest.mock import Mock
from omnibci.sdk import FrameTiming


def make_owner(monkeypatch, failures=()):
    calls = []
    threads = []

    def operation(name):
        def call(*args, **kwargs):
            calls.append(name)
            threads.append(threading.get_ident())
            if name in failures:
                raise RuntimeError(name)
            if name == 'renderer.begin_frame':
                return FrameTiming(1, 1 / 60, 1 / 60)
            if name == 'paradigm.render_frame':
                return True
        return call

    renderer = SimpleNamespace(**{name: operation('renderer.' + name) for name in
        ('initialize', 'shutdown', 'poll_events', 'begin_frame', 'end_frame')})
    renderer.should_close = Mock(side_effect=[False, True])
    trigger = SimpleNamespace(**{name: operation('trigger.' + name) for name in
        ('setup', 'clear', 'teardown', 'send')})
    paradigm = SimpleNamespace(**{name: operation('paradigm.' + name) for name in
        ('teardown', 'render_frame', 'present_trial', 'present_result')})

    def setup(*, renderer, trigger):
        assert renderer is owner._renderer
        assert trigger is owner.trigger
        operation('paradigm.setup')()

    paradigm.setup = setup
    paradigm.is_presenting = lambda: False
    owner = Stimulator(settings_snapshot={})
    monkeypatch.setattr(owner, '_load_trigger', lambda: trigger)
    monkeypatch.setattr(owner, '_load_paradigm', lambda *args: paradigm)
    monkeypatch.setattr('omnibci.infrastructure.stimulation.stimulator._create_renderer', lambda name: renderer)
    return owner, calls, threads, renderer, trigger, paradigm


def errors(error):
    if isinstance(error, BaseExceptionGroup):
        return [item for nested in error.exceptions for item in errors(nested)]
    return [error]


def test_one_thread_and_one_cleanup_owner(monkeypatch):
    owner, calls, threads, *_ = make_owner(monkeypatch)
    asyncio.run(owner.start_async())
    assert calls == [
        'renderer.initialize', 'trigger.setup', 'trigger.clear', 'paradigm.setup',
        'renderer.poll_events', 'renderer.begin_frame', 'paradigm.render_frame', 'renderer.end_frame',
        'paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown',
    ]
    assert set(threads) == {threading.get_ident()}
    owner.request_stop()
    assert calls.count('paradigm.teardown') == 1


@pytest.mark.parametrize('failure,cleanup', [
    ('renderer.initialize', ['renderer.shutdown']),
    ('trigger.setup', ['trigger.teardown', 'renderer.shutdown']),
    ('trigger.clear', ['trigger.clear', 'trigger.teardown', 'renderer.shutdown']),
    ('paradigm.setup', ['paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown']),
    ('renderer.poll_events', ['paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown']),
    ('paradigm.render_frame', ['paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown']),
    ('renderer.end_frame', ['paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown']),
])
def test_partial_start_or_frame_error_releases_owned_resources(monkeypatch, failure, cleanup):
    owner, calls, *_ = make_owner(monkeypatch, {failure})
    with pytest.raises(BaseException) as raised:
        asyncio.run(owner.start_async())
    assert failure in [str(e) for e in errors(raised.value)]
    assert calls[-len(cleanup):] == cleanup


@pytest.mark.parametrize('failures', [
    {'paradigm.teardown'}, {'trigger.teardown'}, {'renderer.shutdown'},
    {'paradigm.render_frame', 'paradigm.teardown', 'trigger.teardown', 'renderer.shutdown'},
])
def test_cleanup_failure_preserves_all_errors_and_blocks_restart(monkeypatch, failures):
    owner, calls, *_ = make_owner(monkeypatch, failures)
    with pytest.raises(BaseException) as raised:
        asyncio.run(owner.start_async())
    assert {str(e) for e in errors(raised.value)} == failures
    assert calls[-4:] == ['paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown']
    previous = list(calls)
    with pytest.raises(RuntimeError, match='cleanup|unsettled'):
        asyncio.run(owner.start_async())
    owner.request_stop()
    assert calls == previous


def test_stop_does_not_discard_running_loop_or_overlap_cleanup(monkeypatch):
    async def scenario():
        owner, calls, _, renderer, *_ = make_owner(monkeypatch)
        renderer.should_close = lambda: False
        task = asyncio.create_task(owner.start_async())
        await asyncio.sleep(0)
        loop = owner._loop
        owner.request_stop()
        assert owner._loop is loop
        assert 'renderer.shutdown' not in calls
        with pytest.raises(RuntimeError, match='running'):
            await owner.start_async()
        await task
        assert calls[-1] == 'renderer.shutdown'
    asyncio.run(scenario())


def test_cancelled_render_task_still_cleans_up_once(monkeypatch):
    async def scenario():
        owner, calls, _, renderer, *_ = make_owner(monkeypatch)
        renderer.should_close = lambda: False
        task = asyncio.create_task(owner.start_async())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls[-4:] == ['paradigm.teardown', 'trigger.clear', 'trigger.teardown', 'renderer.shutdown']
    asyncio.run(scenario())


def test_blocked_callback_retains_resources_until_it_returns(monkeypatch):
    owner, calls, threads, _, _, paradigm = make_owner(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    failures = []

    def blocked_frame(frame):
        entered.set()
        assert release.wait(2)
        return True

    def run():
        try:
            asyncio.run(owner.start_async())
        except BaseException as exc:
            failures.append(exc)

    paradigm.render_frame = blocked_frame
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(1)
        owner.request_stop()
        assert 'paradigm.teardown' not in calls
        with pytest.raises(RuntimeError, match='running'):
            asyncio.run(owner.start_async())
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert failures == []
    assert calls[-1] == 'renderer.shutdown'
    assert set(threads) == {thread.ident}


def test_no_trigger_uses_complete_explicit_lifecycle():
    from omnibci.infrastructure.stimulation.stimulator import _NoTrigger
    output = _NoTrigger()
    assert output.setup() is None
    assert output.send(0) is None
    assert output.clear() is None
    assert output.teardown() is None


def test_public_capabilities_exclude_host_lifecycle():
    from omnibci import sdk
    assert not hasattr(sdk, 'Renderer')
    assert not hasattr(sdk.RenderSurface, 'initialize')
    assert not hasattr(sdk.RenderSurface, 'end_frame')
    assert not hasattr(sdk.TriggerOutput, 'setup')
    assert not hasattr(sdk.TriggerOutput, 'teardown')


@pytest.mark.parametrize('missing', ['setup', 'teardown'])
def test_trigger_cannot_omit_lifecycle(missing):
    from omnibci.domain.ports.acquisition import TriggerEmitter
    methods = {name: lambda *args: None for name in ('setup', 'teardown', 'send', 'clear') if name != missing}
    with pytest.raises(TypeError, match='abstract'):
        type('IncompleteTrigger', (TriggerEmitter,), methods)()


def test_invalid_teardown_attribute_cannot_skip_other_releases(monkeypatch):
    owner, calls, _, _, _, paradigm = make_owner(monkeypatch)
    del paradigm.teardown
    with pytest.raises(AttributeError):
        asyncio.run(owner.start_async())
    assert calls[-3:] == ['trigger.clear', 'trigger.teardown', 'renderer.shutdown']


def test_stop_during_setup_is_observed_before_first_frame(monkeypatch):
    owner, calls, _, renderer, *_ = make_owner(monkeypatch)
    initialize = renderer.initialize

    def stop_during_initialize(**kwargs):
        initialize(**kwargs)
        owner.request_stop()

    renderer.initialize = stop_during_initialize
    asyncio.run(owner.start_async())
    assert 'paradigm.render_frame' not in calls
    assert calls[-1] == 'renderer.shutdown'


@pytest.mark.parametrize('operation', ['setup', 'teardown'])
def test_false_lifecycle_result_cannot_claim_success(monkeypatch, operation):
    owner, calls, _, _, trigger, _ = make_owner(monkeypatch)
    setattr(trigger, operation, lambda: False)
    with pytest.raises(TypeError, match='must return None'):
        asyncio.run(owner.start_async())
    assert calls[-1] == 'renderer.shutdown'
