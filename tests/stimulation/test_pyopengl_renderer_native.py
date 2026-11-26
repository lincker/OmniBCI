"""Exercise PyOpenGL startup and drawing with a real, hidden core context."""
import glfw
import numpy as np
import pytest
from OpenGL.GL import GL_NO_ERROR, GL_RGB, GL_UNSIGNED_BYTE, glGetError, glReadPixels
from OpenGL.GL.shaders import ShaderCompilationError, ShaderLinkError

from omnibci.domain.ports.renderer import Color, Rect
from omnibci.infrastructure.stimulation.adapters import pyopengl_renderer as module


pytestmark = pytest.mark.gui


@pytest.fixture()
def renderer():
    if not glfw.init():
        pytest.skip('A display supporting OpenGL is required')
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    renderer = module.PyOpenGLRenderer()
    try:
        yield renderer
    finally:
        renderer.shutdown()


def _center_pixel(renderer):
    width, height = glfw.get_framebuffer_size(renderer._win.get_window())
    return np.frombuffer(
        glReadPixels(width // 2, height // 2, 1, 1, GL_RGB, GL_UNSIGNED_BYTE),
        dtype=np.uint8,
    )


@pytest.mark.parametrize('run', [1, 2])
def test_start_draw_and_close_fresh_context(renderer, run):
    renderer.initialize(32, 32, f'PyOpenGL regression {run}')
    renderer.clear()
    renderer.draw_rect(Rect(-1, -1, 2, 2), Color(1, 0, 0))
    np.testing.assert_array_equal(_center_pixel(renderer), [255, 0, 0])

    image = np.zeros((2, 2, 3), dtype=np.uint8)
    image[:, :, 1] = 255
    texture = renderer.upload_texture(image)
    renderer.draw_texture(texture, Rect(-1, -1, 2, 2))
    np.testing.assert_array_equal(_center_pixel(renderer), [0, 255, 0])
    renderer.end_frame()
    assert renderer.refresh_rate_hz() > 0
    renderer.release_texture(texture)
    assert glGetError() == GL_NO_ERROR
    renderer.shutdown()


@pytest.mark.parametrize('fragment, error', [
    ('#version 330 core\ninvalid shader', ShaderCompilationError),
    ('''#version 330 core
in vec3 vTexCoord;
out vec4 FragColor;
void main() { FragColor = vec4(vTexCoord, 1.0); }
''', ShaderLinkError),
], ids=['compile', 'link'])
def test_shader_compile_and_link_errors_still_fail_startup(renderer, monkeypatch, fragment, error):
    monkeypatch.setattr(module, '_FRAG_TEXTURE', fragment)
    with pytest.raises(error):
        renderer.initialize(32, 32, 'PyOpenGL invalid shader regression')
