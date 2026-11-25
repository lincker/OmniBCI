#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
import logging
from sys import platform

from OpenGL.GL import glGetString, GL_RENDERER, GL_VERSION, glClearColor, glClear, GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT
import glfw

logger = logging.getLogger(__name__)


def window_refresh_rate_hz(window) -> float | None:
    """Return the refresh rate (Hz) of the monitor containing most of *window*."""
    if not window:
        return None
    monitors = glfw.get_monitors()
    if not monitors:
        return None
    x, y = glfw.get_window_pos(window)
    w, h = glfw.get_window_size(window)
    best = None
    for monitor in monitors:
        mx, my = glfw.get_monitor_pos(monitor)
        mode = glfw.get_video_mode(monitor)
        if mode is None:
            continue
        area = max(0, min(x + w, mx + mode.size.width) - max(x, mx)) * max(
            0, min(y + h, my + mode.size.height) - max(y, my)
        )
        if best is None or area > best[0]:
            best = (area, mode.refresh_rate)
    if best is None or best[1] <= 0:
        return None
    return float(best[1])


class UIWindow:
    def __init__(self, window_name, width, height):
        self.window = None
        self._closed = False
        self._shutdown_error = None
        try:
            if not glfw.init():
                raise RuntimeError('glfw init failed')
            glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
            glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
            glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
            glfw.window_hint(glfw.RESIZABLE, glfw.FALSE)
            if platform == "darwin":
                glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
            self.window = glfw.create_window(width, height, window_name, None, None)
            if not self.window:
                raise RuntimeError('glfw create window failed')
            glfw.make_context_current(self.window)
            glfw.swap_interval(1)
            glfw.set_window_pos(self.window, 0, 0)
            # Clear the framebuffer after establishing the owner context.
            glClearColor(0.0, 0.0, 0.0, 0.0)
            glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
            logger.debug("Renderer: %s", glGetString(GL_RENDERER))
            logger.debug("OpenGL version supported: %s", glGetString(GL_VERSION))
        except BaseException as failure:
            try:
                self.teardown()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup("Window initialization and cleanup failed", [failure, cleanup_error])
            raise

    def get_window(self):
        return self.window

    def teardown(self):
        if self._shutdown_error is not None:
            raise self._shutdown_error
        if self._closed:
            return
        errors = []
        if self.window:
            try:
                glfw.destroy_window(self.window)
            except Exception as exc:
                errors.append(exc)
        try:
            glfw.terminate()
        except Exception as exc:
            errors.append(exc)
        self._closed = True
        if errors:
            self._shutdown_error = ExceptionGroup("Window cleanup failed", errors)
            raise self._shutdown_error

    def should_close(self):
        return glfw.window_should_close(self.window)

    def flip(self):
        glfw.swap_buffers(self.window)

    def poll_events(self):
        glfw.poll_events()
