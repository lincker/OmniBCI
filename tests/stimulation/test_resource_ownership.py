"""The async stimulation owner settles every acquired component exactly once."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omnibci.infrastructure.stimulation.stimulator import Stimulator


def make_owner(monkeypatch, failures=()):
    calls = []
    threads = []

    def operation(name):
        def call(*args, **kwargs):
            calls.append(name)
            threads.append(threading.get_ident())
            if name in failures:
                raise RuntimeError(name)
        return call

    renderer = SimpleNamespace(**{name: operation('renderer.' + name) for name in
        ('initialize', 'shutdown', 'poll_events', 'end_frame')})
    renderer.should_close = Mock(side_effect=[False, True])
    trigger = SimpleNamespace(**{name: operation('trigger.' + name) for name in
        ('setup', 'clear', 'teardown', 'send')})
    paradigm = SimpleNamespace(**{name: operation('paradigm.' + name) for name in
        ('teardown', 'render_frame', 'trial_stimulus_present', 'trial_result_present')})

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
        'renderer.poll_events', 'paradigm.render_frame', 'renderer.end_frame',
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

    def blocked_frame(elapsed):
        entered.set()
        assert release.wait(2)

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
