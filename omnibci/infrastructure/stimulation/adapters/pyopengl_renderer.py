"""
PyOpenGL adapter — implements Renderer protocol by wrapping UIWindow
and raw PyOpenGL calls.

Existing ui/window.py and ui/image.py are left completely untouched.
This adapter is intentionally the "no-optimisation" path: VAO/VBO are
created and destroyed per draw call, which is acceptable at BCI frame
rates (≤ 60 Hz, ≤ 40 rects/frame).  Performance-critical work lives in
ModernGLRenderer (Step 5).

Known issue inherited from UIWindow: glfw.swap_interval(1) is called
before make_context_current — behaviour is driver-dependent.  Fixed in
ModernGLRenderer.
"""
from __future__ import annotations

import ctypes

import numpy as np
from OpenGL.GL import (
    GL_ARRAY_BUFFER, GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT,
    GL_ELEMENT_ARRAY_BUFFER, GL_FLOAT, GL_FALSE,
    GL_NEAREST, GL_ONE, GL_R8, GL_RED, GL_RGB, GL_RGB8, GL_RGBA, GL_RGBA8,
    GL_STATIC_DRAW,
    GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_TEXTURE_MIN_FILTER,
    GL_TEXTURE_SWIZZLE_A, GL_TEXTURE_SWIZZLE_B,
    GL_TEXTURE_SWIZZLE_G, GL_TEXTURE_SWIZZLE_R,
    GL_TRIANGLES, GL_UNSIGNED_BYTE, GL_UNSIGNED_INT,
    GL_UNPACK_ALIGNMENT,
    GL_CLAMP_TO_EDGE, GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T,
    GL_VERTEX_SHADER, GL_FRAGMENT_SHADER,
    glBindBuffer, glBindTexture, glBindVertexArray,
    glBufferData, glClear, glClearColor,
    glDeleteBuffers, glDeleteTextures, glDeleteVertexArrays,
    glDrawElements, glEnableVertexAttribArray,
    glGenBuffers, glGenTextures, glGenVertexArrays,
    glPixelStorei, glTexImage2D, glTexParameteri,
    glUseProgram, glVertexAttribPointer,
    glUniform4f, glGetUniformLocation,
)
from OpenGL.GL.shaders import compileShader, compileProgram

from omnibci.infrastructure.stimulation.ports.renderer import (
    Color, Rect, TextureHandle,
)
from omnibci.infrastructure.stimulation.ui.window import UIWindow


# ---------------------------------------------------------------------------
# GLSL sources
# ---------------------------------------------------------------------------

_VERT_TEXTURE = """
#version 330 core
layout(location = 0) in vec2 aPos;
layout(location = 1) in vec2 aTexCoord;
out vec2 vTexCoord;
void main() {
    gl_Position = vec4(aPos, 0.0, 1.0);
    vTexCoord   = aTexCoord;
}
"""

_FRAG_TEXTURE = """
#version 330 core
in  vec2 vTexCoord;
out vec4 FragColor;
uniform sampler2D uTexture;
void main() {
    FragColor = texture(uTexture, vTexCoord);
}
"""

_VERT_COLOR = """
#version 330 core
layout(location = 0) in vec2 aPos;
void main() {
    gl_Position = vec4(aPos, 0.0, 1.0);
}
"""

_FRAG_COLOR = """
#version 330 core
out vec4 FragColor;
uniform vec4 uColor;
void main() {
    FragColor = uColor;
}
"""

# Two CCW triangles forming a quad — shared by both shaders
_QUAD_INDICES = np.array([0, 1, 2, 2, 3, 0], dtype=np.uint32)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class PyOpenGLRenderer:
    """Renderer backed by PyOpenGL 3.3 Core Profile + GLFW (via UIWindow)."""

    # -- Lifecycle -----------------------------------------------------------

    def initialize(self, width: int, height: int, title: str) -> None:
        self._width  = width
        self._height = height
        self._win    = UIWindow(title, width, height)

        # handle_id (int) → GL texture name (int)
        self._textures: dict[int, int] = {}
        self._next_id = 1

        self._setup_programs()
        self._setup_shared_ebo()

    def shutdown(self) -> None:
        glDeleteBuffers(1, [self._ebo])
        self._win.teardown()

    def poll_events(self) -> None:
        self._win.poll_events()

    def should_close(self) -> bool:
        return self._win.should_close()

    # -- Texture management --------------------------------------------------

    def upload_texture(self, image: np.ndarray) -> TextureHandle:
        """Upload an H×W luminance, RGB, or RGBA image."""
        if not isinstance(image, np.ndarray):
            raise TypeError("Texture image must be a numpy.ndarray")
        if image.dtype != np.uint8:
            raise TypeError(f"Texture image dtype must be uint8, got {image.dtype}")
        if image.ndim != 3 or image.shape[2] not in (1, 3, 4):
            raise ValueError(
                "Texture image shape must be (H, W, C) with C in (1, 3, 4), "
                f"got {image.shape}"
            )
        image = np.ascontiguousarray(image)
        h, w, components = image.shape
        formats = {
            1: (GL_R8, GL_RED),
            3: (GL_RGB8, GL_RGB),
            4: (GL_RGBA8, GL_RGBA),
        }
        internal_format, pixel_format = formats[components]

        tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S,     GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T,     GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        if components == 1:
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_SWIZZLE_R, GL_RED)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_SWIZZLE_G, GL_RED)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_SWIZZLE_B, GL_RED)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_SWIZZLE_A, GL_ONE)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexImage2D(
            GL_TEXTURE_2D, 0, internal_format, w, h, 0,
            pixel_format, GL_UNSIGNED_BYTE, image,
        )
        glBindTexture(GL_TEXTURE_2D, 0)

        handle = TextureHandle(id=self._next_id)
        self._textures[self._next_id] = tex
        self._next_id += 1
        return handle

    def release_texture(self, handle: TextureHandle) -> None:
        tex = self._textures.pop(handle.id, None)
        if tex is not None:
            glDeleteTextures(1, [tex])

    # -- Drawing -------------------------------------------------------------

    def clear(self, color: Color = Color(0.0, 0.0, 0.0)) -> None:
        glClearColor(color.r, color.g, color.b, color.a)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)

    def draw_texture(self, handle: TextureHandle, rect: Rect) -> None:
        tex = self._textures[handle.id]
        vao, vbo = self._build_textured_quad(rect)
        glUseProgram(self._prog_texture)
        glBindTexture(GL_TEXTURE_2D, tex)
        glBindVertexArray(vao)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self._ebo)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)
        glDeleteVertexArrays(1, [vao])
        glDeleteBuffers(1, [vbo])

    def draw_rect(self, rect: Rect, color: Color) -> None:
        vao, vbo = self._build_colored_quad(rect)
        glUseProgram(self._prog_color)
        glUniform4f(
            glGetUniformLocation(self._prog_color, "uColor"),
            color.r, color.g, color.b, color.a,
        )
        glBindVertexArray(vao)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self._ebo)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)
        glBindVertexArray(0)
        glDeleteVertexArrays(1, [vao])
        glDeleteBuffers(1, [vbo])

    # -- Frame submission ----------------------------------------------------

    def end_frame(self) -> None:
        self._win.flip()

    # -- Internal setup ------------------------------------------------------

    def _setup_programs(self) -> None:
        self._prog_texture = compileProgram(
            compileShader(_VERT_TEXTURE, GL_VERTEX_SHADER),
            compileShader(_FRAG_TEXTURE, GL_FRAGMENT_SHADER),
        )
        self._prog_color = compileProgram(
            compileShader(_VERT_COLOR, GL_VERTEX_SHADER),
            compileShader(_FRAG_COLOR, GL_FRAGMENT_SHADER),
        )

    def _setup_shared_ebo(self) -> None:
        """Upload the shared quad index buffer once."""
        self._ebo = glGenBuffers(1)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self._ebo)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER,
                     _QUAD_INDICES.nbytes, _QUAD_INDICES, GL_STATIC_DRAW)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)

    def _build_textured_quad(self, rect: Rect):
        """VAO + VBO for a textured quad at *rect* (NDC).

        Vertex layout: [x, y, u, v] — 4 floats per vertex.
        """
        x, y, w, h = rect
        verts = np.array([
            x,     y,     0.0, 0.0,   # bottom-left
            x + w, y,     1.0, 0.0,   # bottom-right
            x + w, y + h, 1.0, 1.0,   # top-right
            x,     y + h, 0.0, 1.0,   # top-left
        ], dtype=np.float32)
        return self._upload_vao(verts, stride=4, attribs=[(0, 2, 0), (1, 2, 2)])

    def _build_colored_quad(self, rect: Rect):
        """VAO + VBO for a solid-color quad at *rect* (NDC).

        Vertex layout: [x, y] — 2 floats per vertex.
        """
        x, y, w, h = rect
        verts = np.array([
            x,     y,
            x + w, y,
            x + w, y + h,
            x,     y + h,
        ], dtype=np.float32)
        return self._upload_vao(verts, stride=2, attribs=[(0, 2, 0)])

    @staticmethod
    def _upload_vao(verts: np.ndarray, stride: int,
                    attribs: list[tuple[int, int, int]]):
        """Create a VAO+VBO from *verts*.

        Parameters
        ----------
        verts:   flat float32 vertex data
        stride:  number of floats per vertex
        attribs: list of (location, size, offset_in_floats)
        """
        vao = glGenVertexArrays(1)
        vbo = glGenBuffers(1)
        glBindVertexArray(vao)
        glBindBuffer(GL_ARRAY_BUFFER, vbo)
        glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STATIC_DRAW)
        byte_stride = stride * 4
        for loc, size, offset in attribs:
            glVertexAttribPointer(
                loc, size, GL_FLOAT, GL_FALSE,
                byte_stride, ctypes.c_void_p(offset * 4),
            )
            glEnableVertexAttribArray(loc)
        glBindBuffer(GL_ARRAY_BUFFER, 0)
        glBindVertexArray(0)
        return vao, vbo
