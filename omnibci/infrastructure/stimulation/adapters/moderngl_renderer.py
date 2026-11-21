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

from omnibci.domain.ports.renderer import (
    Color, FrameTiming, Rect, TextureHandle,
)
from omnibci.infrastructure.stimulation.frame_clock import create_frame_clock


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

    def __init__(self) -> None:
        self._glfw_started = False
        self._window = None
        self._clock = None
        self._textures: dict[TextureHandle, mgl.Texture] = {}
        self._texture_owner = object()
        self._started = False
        self._ready = False
        self._closed = False
        self._shutdown_error = None
        for name in ("_ctx", "_prog_tex", "_prog_col", "_ibo", "_vbo_tex", "_vbo_col", "_vao_tex", "_vao_col"):
            setattr(self, name, None)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def initialize(self, width: int, height: int, title: str) -> None:
        if self._started or self._closed:
            raise RuntimeError("Renderer already started or closed")
        self._started = True
        self._glfw_started = True
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
            raise RuntimeError("glfw.create_window() failed")

        glfw.make_context_current(self._window)
        glfw.swap_interval(1)           # Request synchronized swaps; clock samples gate drawing.
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

        self._clock = create_frame_clock(self._window)
        self._ready = True

    def shutdown(self) -> None:
        if self._shutdown_error is not None:
            raise self._shutdown_error
        if self._closed:
            return
        errors = []
        if self._clock is not None:
            try:
                self._clock.close()
            except Exception as exc:
                errors.append(exc)
        resources = list(self._textures.values()) + [getattr(self, name) for name in (
            "_vao_tex", "_vao_col", "_vbo_tex", "_vbo_col", "_ibo", "_prog_tex", "_prog_col", "_ctx")]
        for resource in resources:
            if resource is not None:
                try:
                    resource.release()
                except Exception as exc:
                    errors.append(exc)
        if self._window:
            try:
                glfw.destroy_window(self._window)
            except Exception as exc:
                errors.append(exc)
        if self._glfw_started:
            try:
                glfw.terminate()
            except Exception as exc:
                errors.append(exc)
        self._closed = True
        self._textures.clear()
        if errors:
            self._shutdown_error = ExceptionGroup("ModernGL resource cleanup failed", errors)
            raise self._shutdown_error

    # ── Window ─────────────────────────────────────────────────────────────

    def poll_events(self) -> None:
        self._ensure_open()
        glfw.poll_events()

    def should_close(self) -> bool:
        self._ensure_open()
        return glfw.window_should_close(self._window)

    def begin_frame(self) -> FrameTiming | None:
        self._ensure_open()
        return self._clock.begin_frame()

    def end_frame(self, frame: FrameTiming, *, submit: bool) -> bool:
        self._ensure_open()
        if not self._clock.end_frame(frame, submit=submit):
            return False
        glfw.swap_buffers(self._window)
        return True

    # ── Texture management ──────────────────────────────────────────────────

    def upload_texture(self, image: np.ndarray) -> TextureHandle:
        """Upload an H×W luminance, RGB, or RGBA image."""
        self._ensure_open()
        if not isinstance(image, np.ndarray):
            raise TypeError("Texture image must be a numpy.ndarray")
        if image.dtype != np.uint8:
            raise TypeError(f"Texture image dtype must be uint8, got {image.dtype}")
        if image.ndim != 3 or min(image.shape[:2]) <= 0 or image.shape[2] not in (1, 3, 4):
            raise ValueError(
                "Texture image shape must be positive-size (H, W, C) with C in (1, 3, 4), "
                f"got {image.shape}"
            )
        image = np.ascontiguousarray(image)
        h, w, components = image.shape
        tex = self._ctx.texture((w, h), components, image.tobytes())
        handle = TextureHandle(_owner=self._texture_owner)
        self._textures[handle] = tex
        try:
            tex.filter = (mgl.NEAREST, mgl.NEAREST)
            if components == 1:
                tex.swizzle = "RRR1"
        except BaseException as failure:
            try:
                self.release_texture(handle)
            except BaseException as cleanup:
                raise BaseExceptionGroup("Texture upload and cleanup failed", [failure, cleanup])
            raise
        return handle

    def _ensure_open(self) -> None:
        if self._closed or not self._ready:
            raise RuntimeError("Renderer is closed or not initialized")

    def _check_handle(self, handle: TextureHandle) -> None:
        self._ensure_open()
        if not isinstance(handle, TextureHandle) or handle._owner is not self._texture_owner:
            raise ValueError("Texture handle belongs to another surface")

    def release_texture(self, handle: TextureHandle) -> None:
        self._check_handle(handle)
        tex = self._textures.get(handle)
        if tex is not None:
            tex.release()
            del self._textures[handle]

    # ── Drawing ─────────────────────────────────────────────────────────────

    def clear(self, color: Color = Color(0.0, 0.0, 0.0)) -> None:
        self._ensure_open()
        self._ctx.clear(color.r, color.g, color.b, color.a)

    def draw_texture(self, handle: TextureHandle, rect: Rect) -> None:
        self._check_handle(handle)
        if handle not in self._textures:
            raise ValueError("Texture handle has been released")
        x, y, w, h = rect.x, rect.y, rect.w, rect.h
        verts = np.array([
            x,     y,     0.0, 0.0,   # bottom-left
            x + w, y,     1.0, 0.0,   # bottom-right
            x + w, y + h, 1.0, 1.0,   # top-right
            x,     y + h, 0.0, 1.0,   # top-left
        ], dtype=np.float32)
        self._vbo_tex.write(verts.tobytes())
        self._textures[handle].use(location=0)
        self._prog_tex["uTexture"].value = 0
        self._vao_tex.render(mgl.TRIANGLES)

    def draw_rect(self, rect: Rect, color: Color) -> None:
        self._ensure_open()
        x, y, w, h = rect.x, rect.y, rect.w, rect.h
        verts = np.array([
            x,     y,
            x + w, y,
            x + w, y + h,
            x,     y + h,
        ], dtype=np.float32)
        self._vbo_col.write(verts.tobytes())
        self._prog_col["uColor"].value = (color.r, color.g, color.b, color.a)
        self._vao_col.render(mgl.TRIANGLES)
