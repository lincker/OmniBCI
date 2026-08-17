#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
"""ModernGL adapter — implements Renderer protocol via moderngl + GLFW.

Advantages over PyOpenGLRenderer:
- Reusable dynamic VBOs: one ``vbo.write()`` per draw, no per-draw VAO/VBO
  creation and deletion.
- Object-oriented moderngl API: no ctypes, no manual attrib pointers.
- GLSL shaders are identical to PyOpenGLRenderer — zero shader divergence.

Requires:
    pip install moderngl pyglfw
"""
from __future__ import annotations

import sys

import numpy as np
import moderngl as mgl
import glfw

from omnibci.infrastructure.stimulation.ports.renderer import (
    Color, Rect, TextureHandle,
)


# ---------------------------------------------------------------------------
# GLSL sources (identical to PyOpenGLRenderer)
# ---------------------------------------------------------------------------

_VERT_TEXTURE = """
#version 330 core
in vec2 aPos;
in vec2 aTexCoord;
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
in vec2 aPos;
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

_INDICES = np.array([0, 1, 2, 2, 3, 0], dtype=np.uint32)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ModernGLRenderer:
    """Renderer backed by moderngl (OpenGL 3.3 Core Profile) + GLFW."""

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def initialize(self, width: int, height: int, title: str) -> None:
        if not glfw.init():
            raise RuntimeError("glfw.init() failed")

        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.RESIZABLE, glfw.FALSE)
        if sys.platform == "darwin":
            glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)

        self._window = glfw.create_window(width, height, title, None, None)
        if not self._window:
            glfw.terminate()
            raise RuntimeError("glfw.create_window() failed")

        glfw.make_context_current(self._window)
        glfw.swap_interval(1)           # VSync: lock to display refresh rate
        glfw.set_window_pos(self._window, 0, 0)

        # moderngl wraps the current GL context
        self._ctx = mgl.create_context()
        self._ctx.enable(mgl.BLEND)
        self._ctx.blend_func = mgl.SRC_ALPHA, mgl.ONE_MINUS_SRC_ALPHA

        # Compile programs
        self._prog_tex = self._ctx.program(
            vertex_shader=_VERT_TEXTURE,
            fragment_shader=_FRAG_TEXTURE,
        )
        self._prog_col = self._ctx.program(
            vertex_shader=_VERT_COLOR,
            fragment_shader=_FRAG_COLOR,
        )

        # Shared index buffer (two CCW triangles per quad)
        self._ibo = self._ctx.buffer(_INDICES.tobytes())

        # Reusable dynamic VBOs — written fresh on each draw call.
        # Textured quad: 4 verts × [x, y, u, v] = 64 bytes
        self._vbo_tex = self._ctx.buffer(reserve=4 * 4 * 4, dynamic=True)
        # Colored quad:  4 verts × [x, y]        = 32 bytes
        self._vbo_col = self._ctx.buffer(reserve=4 * 2 * 4, dynamic=True)

        self._vao_tex = self._ctx.vertex_array(
            self._prog_tex,
            [(self._vbo_tex, "2f 2f", "aPos", "aTexCoord")],
            self._ibo,
        )
        self._vao_col = self._ctx.vertex_array(
            self._prog_col,
            [(self._vbo_col, "2f", "aPos")],
            self._ibo,
        )

        # handle_id (int) → mgl.Texture
        self._textures: dict[int, mgl.Texture] = {}
        self._next_id = 1

    def shutdown(self) -> None:
        for tex in self._textures.values():
            tex.release()
        self._vao_tex.release()
        self._vao_col.release()
        self._vbo_tex.release()
        self._vbo_col.release()
        self._ibo.release()
        self._prog_tex.release()
        self._prog_col.release()
        self._ctx.release()
        glfw.terminate()

    # ── Window ─────────────────────────────────────────────────────────────

    def poll_events(self) -> None:
        glfw.poll_events()

    def should_close(self) -> bool:
        return glfw.window_should_close(self._window)

    def end_frame(self) -> None:
        glfw.swap_buffers(self._window)

    # ── Texture management ──────────────────────────────────────────────────

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
        tex = self._ctx.texture((w, h), components, image.tobytes())
        tex.filter = (mgl.NEAREST, mgl.NEAREST)
        if components == 1:
            tex.swizzle = "RRR1"
        handle = TextureHandle(id=self._next_id)
        self._textures[self._next_id] = tex
        self._next_id += 1
        return handle

    def release_texture(self, handle: TextureHandle) -> None:
        tex = self._textures.pop(handle.id, None)
        if tex is not None:
            tex.release()

    # ── Drawing ─────────────────────────────────────────────────────────────

    def clear(self, color: Color = Color(0.0, 0.0, 0.0)) -> None:
        self._ctx.clear(color.r, color.g, color.b, color.a)

    def draw_texture(self, handle: TextureHandle, rect: Rect) -> None:
        x, y, w, h = rect
        verts = np.array([
            x,     y,     0.0, 0.0,   # bottom-left
            x + w, y,     1.0, 0.0,   # bottom-right
            x + w, y + h, 1.0, 1.0,   # top-right
            x,     y + h, 0.0, 1.0,   # top-left
        ], dtype=np.float32)
        self._vbo_tex.write(verts.tobytes())
        self._textures[handle.id].use(location=0)
        self._prog_tex["uTexture"].value = 0
        self._vao_tex.render(mgl.TRIANGLES)

    def draw_rect(self, rect: Rect, color: Color) -> None:
        x, y, w, h = rect
        verts = np.array([
            x,     y,
            x + w, y,
            x + w, y + h,
            x,     y + h,
        ], dtype=np.float32)
        self._vbo_col.write(verts.tobytes())
        self._prog_col["uColor"].value = (color.r, color.g, color.b, color.a)
        self._vao_col.render(mgl.TRIANGLES)
