#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# from UIview import UIView
from OpenGL.GL import glGetString, GL_RENDERER, GL_VERSION,glClearColor, glClear, GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT
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
        glfw.set_window_pos(self.window, 0, 0)
        # 设置 OpenGL 清除颜色为黑色 (R=0, G=0, B=0, A=1)
        glClearColor(0.0, 0.0, 0.0, 0.0)
        glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT)  # 清除颜色缓冲区
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
