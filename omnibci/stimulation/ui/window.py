#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# from UIview import UIView
from OpenGL.GL import glGetString, GL_RENDERER, GL_VERSION,glViewport
import glfw
from sys import platform


class UIWindow:
    root_view_controller = None

    def __init__(self, window_name, width, height):
        if not glfw.init():
            print('glfw init failed')
            return
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.RESIZABLE, glfw.FALSE)
        glfw.swap_interval(1)
        if platform == "darwin":
            glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
        self.window = glfw.create_window(width, height, window_name, None, None)
        if not self.window:
            glfw.terminate()
            print('glfw create window failed')
            return
        glfw.make_context_current(self.window)
        # glViewport(0, 0, width, height)
        # OpenGL version info
        renderer = glGetString(GL_RENDERER)
        version = glGetString(GL_VERSION)
        print('Renderer:', renderer)  # Renderer: b'Intel Iris Pro OpenGL Engine'
        print('OpenGL version supported: ', version)  # OpenGL version supported:  b'4.1 INTEL-10.12.13'

    def get_window(self):
        return self.window

    def teardown(self):
        glfw.terminate()

    def should_close(self):
        return glfw.window_should_close(self.window)

    def flip(self):
        glfw.swap_buffers(self.window)

    def poll_events(self):
        glfw.poll_events()
