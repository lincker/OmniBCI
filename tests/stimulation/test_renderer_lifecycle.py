"""Both renderer adapters release partial resources without a display server."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('adapter', ['pyopengl_renderer', 'moderngl_renderer'])
def test_shutdown_before_initialization_is_safe(adapter):
    import importlib
    module = importlib.import_module('omnibci.infrastructure.stimulation.adapters.' + adapter)
    implementation = module.PyOpenGLRenderer if adapter.startswith('py') else module.ModernGLRenderer
    implementation().shutdown()


def test_pyopengl_partial_setup_keeps_window_and_completed_program(monkeypatch):
    from omnibci.infrastructure.stimulation.adapters import pyopengl_renderer as module
    window = Mock()
    monkeypatch.setattr(module, 'UIWindow', Mock(return_value=window))
    monkeypatch.setattr(module, 'compileShader', Mock(return_value=1))
    monkeypatch.setattr(module, 'compileProgram', Mock(side_effect=[11, RuntimeError('second program failed')]))
    delete = Mock()
    monkeypatch.setattr(module, 'glDeleteProgram', delete, raising=False)
    renderer = module.PyOpenGLRenderer()
    with pytest.raises(RuntimeError, match='second program failed'):
        renderer.initialize(10, 10, 'test')
    renderer.shutdown()
    delete.assert_called_once_with(11)
    window.teardown.assert_called_once()
    renderer.shutdown()
    window.teardown.assert_called_once()


def test_pyopengl_release_failure_still_closes_window(monkeypatch):
    from omnibci.infrastructure.stimulation.adapters import pyopengl_renderer as module
    window = Mock()
    renderer = module.PyOpenGLRenderer()
    renderer._win = window
    renderer._ebo = 12
    monkeypatch.setattr(module, 'glDeleteBuffers', Mock(side_effect=RuntimeError('buffer release failed')))
    with pytest.raises(BaseException, match='release|cleanup'):
        renderer.shutdown()
    window.teardown.assert_called_once()


@pytest.mark.parametrize('release_error', [False, True])
def test_moderngl_partial_setup_releases_completed_resources_and_window(monkeypatch, release_error):
    from omnibci.infrastructure.stimulation.adapters import moderngl_renderer as module
    glfw = Mock()
    ctx = Mock()
    program = Mock()
    if release_error:
        program.release.side_effect = RuntimeError('program release failed')
    ctx.program.side_effect = [program, RuntimeError('second program failed')]
    monkeypatch.setattr(module, 'glfw', glfw)
    monkeypatch.setattr(module.mgl, 'create_context', Mock(return_value=ctx))
    renderer = module.ModernGLRenderer()
    with pytest.raises(RuntimeError, match='second program failed'):
        renderer.initialize(10, 10, 'test')
    if release_error:
        with pytest.raises(BaseException, match='release|cleanup'):
            renderer.shutdown()
    else:
        renderer.shutdown()
        renderer.shutdown()
    program.release.assert_called_once()
    ctx.release.assert_called_once()
    glfw.destroy_window.assert_called_once_with(glfw.create_window.return_value)
    glfw.terminate.assert_called_once()


@pytest.mark.parametrize('failure', ['init', 'create_window', 'make_context_current'])
def test_window_constructor_raises_and_releases_partial_initialization(monkeypatch, failure):
    from omnibci.infrastructure.stimulation.ui import window as module
    glfw = Mock()
    if failure in ('init', 'create_window'):
        getattr(glfw, failure).return_value = False
    else:
        glfw.make_context_current.side_effect = RuntimeError('context failed')
    monkeypatch.setattr(module, 'glfw', glfw)
    with pytest.raises(RuntimeError):
        module.UIWindow('test', 10, 10)
    glfw.terminate.assert_called_once()
    if failure == 'make_context_current':
        glfw.destroy_window.assert_called_once_with(glfw.create_window.return_value)


def test_window_current_context_precedes_vsync(monkeypatch):
    from omnibci.infrastructure.stimulation.ui import window as module
    glfw = Mock()
    monkeypatch.setattr(module, 'glfw', glfw)
    for name in ('glGetString', 'glClearColor', 'glClear'):
        monkeypatch.setattr(module, name, Mock())
    window = module.UIWindow('test', 10, 10)
    names = [call[0] for call in glfw.mock_calls]
    assert names.index('make_context_current') < names.index('swap_interval')
    window.teardown()


def test_window_refresh_rate_selects_monitor_with_largest_overlap(monkeypatch):
    from omnibci.infrastructure.stimulation.ui import window as module
    primary = object()
    secondary = object()
    glfw = Mock()
    glfw.get_monitors.return_value = [primary, secondary]
    glfw.get_window_pos.return_value = (1920, 0)
    glfw.get_window_size.return_value = (1920, 1080)
    glfw.get_monitor_pos.side_effect = lambda m: (0, 0) if m is primary else (1920, 0)
    glfw.get_video_mode.side_effect = lambda m: SimpleNamespace(
        size=SimpleNamespace(width=1920, height=1080),
        refresh_rate=120 if m is primary else 60,
    )
    monkeypatch.setattr(module, 'glfw', glfw)
    assert module.window_refresh_rate_hz(object()) == 60.0

    glfw.get_monitors.return_value = []
    assert module.window_refresh_rate_hz(object()) is None
