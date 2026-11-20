"""Opaque texture lifetime and input validation for both GPU adapters, using fakes."""
import importlib
from unittest.mock import MagicMock, Mock

import numpy as np
import pytest

from omnibci.domain.ports.renderer import Rect, TextureHandle


@pytest.fixture(params=['pyopengl', 'moderngl'])
def surfaces(request, monkeypatch):
    module = importlib.import_module(
        'omnibci.infrastructure.stimulation.adapters.' + request.param + '_renderer')
    if request.param == 'pyopengl':
        implementation = module.PyOpenGLRenderer
        for name, value in vars(module).copy().items():
            if name.startswith('gl') and callable(value):
                monkeypatch.setattr(module, name, Mock())
        ids = iter(range(10, 10000))
        for name in ('glGenTextures', 'glGenVertexArrays', 'glGenBuffers'):
            monkeypatch.setattr(module, name, Mock(side_effect=lambda *args: next(ids)))
        monkeypatch.setattr(module, 'UIWindow', Mock(side_effect=lambda *args: Mock()))
        monkeypatch.setattr(implementation, '_setup_programs', lambda self: None)
    else:
        implementation = module.ModernGLRenderer
        monkeypatch.setattr(module, 'glfw', Mock())
        ctx = MagicMock()
        ctx.texture.side_effect = lambda *args, **kwargs: Mock()
        monkeypatch.setattr(module.mgl, 'create_context', Mock(return_value=ctx))
    created = []

    def create():
        surface = implementation()
        surface.initialize(16, 16, 'test')
        created.append(surface)
        return surface

    yield create, module
    for surface in created:
        try:
            surface.shutdown()
        except Exception:
            # Individual tests assert failed cleanup; this fixture adds no retry.
            pass


def image(channels=1):
    return np.zeros((4, 5, channels), dtype=np.uint8)


def test_uploaded_handle_exposes_no_numeric_gpu_id(surfaces):
    create, _ = surfaces
    surface = create()
    handle = surface.upload_texture(image())
    assert not hasattr(handle, 'id')
    assert not isinstance(handle, (int, tuple))
    surface.draw_texture(handle, Rect(-1, -1, 2, 2))


def test_surface_rejects_foreign_handles_for_draw_and_release(surfaces):
    create, _ = surfaces
    first, second = create(), create()
    foreign = first.upload_texture(image())
    own = second.upload_texture(image())
    with pytest.raises(ValueError, match='handle|surface'):
        second.draw_texture(foreign, Rect(-1, -1, 2, 2))
    with pytest.raises(ValueError, match='handle|surface'):
        second.release_texture(foreign)
    second.draw_texture(own, Rect(-1, -1, 2, 2))
    first.draw_texture(foreign, Rect(-1, -1, 2, 2))


def test_release_is_idempotent_but_released_handle_cannot_draw(surfaces):
    create, _ = surfaces
    surface = create()
    handle = surface.upload_texture(image())
    surface.release_texture(handle)
    surface.release_texture(handle)
    with pytest.raises(ValueError, match='released|handle'):
        surface.draw_texture(handle, Rect(-1, -1, 2, 2))


def test_shutdown_invalidates_handles_and_rejects_new_uploads(surfaces):
    create, _ = surfaces
    surface = create()
    handle = surface.upload_texture(image())
    surface.shutdown()
    with pytest.raises((ValueError, RuntimeError), match='closed|handle|shutdown'):
        surface.draw_texture(handle, Rect(-1, -1, 2, 2))
    with pytest.raises((ValueError, RuntimeError), match='closed|handle|shutdown'):
        surface.release_texture(handle)
    with pytest.raises(RuntimeError, match='closed|shutdown'):
        surface.upload_texture(image())


@pytest.mark.parametrize('shape', [(0, 4, 1), (4, 0, 1), (0, 0, 4), (2, 2, 2), (2, 2), (2, 2, 5)])
def test_invalid_dimensions_never_allocate_a_texture(surfaces, shape):
    create, module = surfaces
    surface = create()
    allocate = module.glGenTextures if hasattr(module, 'glGenTextures') else surface._ctx.texture
    allocate.reset_mock()
    with pytest.raises(ValueError):
        surface.upload_texture(np.zeros(shape, dtype=np.uint8))
    allocate.assert_not_called()


@pytest.mark.parametrize('channels', [1, 3, 4])
def test_noncontiguous_uint8_image_is_uploaded_without_mutating_it(surfaces, channels):
    create, _ = surfaces
    surface = create()
    data = np.arange(4 * 6 * channels, dtype=np.uint8).reshape(4, 6, channels)[:, ::2]
    before = data.copy()
    handle = surface.upload_texture(data)
    surface.draw_texture(handle, Rect(-1, -1, 2, 2))
    np.testing.assert_array_equal(data, before)
    surface.release_texture(handle)


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_failed_upload_releases_allocated_texture(surfaces, cleanup_fails):
    create, module = surfaces
    surface = create()
    if hasattr(module, 'glTexImage2D'):
        module.glTexImage2D.side_effect = RuntimeError('upload failed')
        released = module.glDeleteTextures
    else:
        class Texture:
            release = Mock()
            @property
            def filter(self):
                return None
            @filter.setter
            def filter(self, value):
                raise RuntimeError('upload failed')
        texture = Texture()
        surface._ctx.texture.side_effect = None
        surface._ctx.texture.return_value = texture
        released = texture.release
    if cleanup_fails:
        released.side_effect = RuntimeError('release failed')
    expected = ExceptionGroup if cleanup_fails else RuntimeError
    with pytest.raises(expected) as caught:
        surface.upload_texture(image())
    if cleanup_fails:
        assert [str(error) for error in caught.value.exceptions] == ['upload failed', 'release failed']
    else:
        assert str(caught.value) == 'upload failed'
    released.assert_called_once()
    released.side_effect = None
    surface.shutdown()
    assert released.call_count == (2 if cleanup_fails else 1)


def test_failed_release_remains_owned_until_shutdown(surfaces):
    create, module = surfaces
    surface = create()
    handle = surface.upload_texture(image())
    released = module.glDeleteTextures if hasattr(module, 'glDeleteTextures') else surface._textures[handle].release
    released.side_effect = RuntimeError('release failed')
    with pytest.raises(RuntimeError, match='release failed'):
        surface.release_texture(handle)
    released.side_effect = None
    surface.shutdown()
    assert released.call_count == 2


def test_fabricated_handles_are_rejected(surfaces):
    create, _ = surfaces
    surface = create()
    for handle in (TextureHandle(), object(), 1):
        with pytest.raises(ValueError, match='handle'):
            surface.draw_texture(handle, Rect(-1, -1, 2, 2))
        with pytest.raises(ValueError, match='handle'):
            surface.release_texture(handle)


def test_initialization_cannot_replace_an_owned_or_closed_context(surfaces):
    create, _ = surfaces
    surface = create()
    handle = surface.upload_texture(image())
    with pytest.raises(RuntimeError, match='initialized|started'):
        surface.initialize(8, 8, 'replacement')
    surface.draw_texture(handle, Rect(-1, -1, 2, 2))
    surface.shutdown()
    with pytest.raises(RuntimeError, match='closed|initialized|started'):
        surface.initialize(8, 8, 'replacement')
