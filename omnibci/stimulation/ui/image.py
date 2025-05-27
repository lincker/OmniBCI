#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-09-04 09:51
import numpy as np
from PIL import Image
from OpenGL.GL import *


# Shaders
vertex_shader1 = """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aColor;
layout (location = 2) in vec2 aTexCoord;

out vec3 ourColor;
out vec2 TexCoord;

void main()
{
    gl_Position = vec4(aPos, 1.0);
    ourColor = aColor;
    TexCoord = vec2(aTexCoord.x, aTexCoord.y);
}"""

fragment_shader1 = """#version 330 core
out vec4 FragColor;

in vec3 ourColor;
in vec2 TexCoord;

// texture sampler
uniform sampler2D texture1;

void main()
{
    FragColor = texture(texture1, TexCoord);
}"""


def create_shader(vertex_shader, fragment_shader):
    vs_id = GLuint(glCreateShader(GL_VERTEX_SHADER))  # shader id for vertex shader
    glShaderSource(vs_id, [vertex_shader], None)  # Send the code of the shader
    glCompileShader(vs_id)  # compile the shader code
    status = glGetShaderiv(vs_id, GL_COMPILE_STATUS)
    if status != 1:
        print('VERTEX SHADER ERROR')
        print(glGetShaderInfoLog(vs_id).decode())

    fs_id = GLuint(glCreateShader(GL_FRAGMENT_SHADER))
    glShaderSource(fs_id, [fragment_shader], None)
    glCompileShader(fs_id)
    status = glGetShaderiv(fs_id, GL_COMPILE_STATUS)
    if status != 1:
        print('FRAGMENT SHADER ERROR')
        print(glGetShaderInfoLog(fs_id).decode())

    # Link the shaders into a single program
    program_id = GLuint(glCreateProgram())
    glAttachShader(program_id, vs_id)
    glAttachShader(program_id, fs_id)
    glLinkProgram(program_id)
    status = glGetProgramiv(program_id, GL_LINK_STATUS)
    if status != 1:
        print('status', status)
        print('SHADER PROGRAM', glGetShaderInfoLog(program_id))

    glDeleteShader(vs_id)
    glDeleteShader(fs_id)

    return program_id


class StimImage:
    def __init__(self, image_path: str = None, image: Image = None):
        # self.window = window
        if image:
            self.image = image
        else:
            self.image = self._load_image(path=image_path)
        self.texture = self._create_texture(self.image)

    def draw(self):
        self._draw_texture(self.texture)

    def _load_image(self, path):
        try:
            image = Image.open(path)
        except FileNotFoundError:
            raise FileNotFoundError(f"Image {path} not found!")
        return image

    def _create_texture(self, image):
        RGBImage = image.convert('RGB')
        # image upside down
        RGBImage = RGBImage.transpose(Image.FLIP_TOP_BOTTOM)
        # convert to numpy array
        image_data = np.array(RGBImage)

        self._set_VAO()

        texture = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, texture)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)  # GL_LINEAR
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)  # GL_LINEAR
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, image.size[0], image.size[1], 0, GL_RGB, GL_UNSIGNED_BYTE, image_data)
        RGBImage.close()
        return texture


    def _set_VAO(self):
        self.vertices = np.array([
            1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0,
            1.0, -1.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0,
            -1.0, -1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
            -1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0
        ], dtype=np.float32)
        self.indices = np.array([
            0, 1, 3,
            1, 2, 3
        ], dtype=np.uint32)

        self.VAO = glGenVertexArrays(1)
        self.VBO = glGenBuffers(1)
        self.EBO = glGenBuffers(1)
        glBindVertexArray(self.VAO)
        glBindBuffer(GL_ARRAY_BUFFER, self.VBO)
        glBufferData(GL_ARRAY_BUFFER, len(self.vertices) * 4, self.vertices, GL_STATIC_DRAW)
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, len(self.indices) * 4, self.indices, GL_STATIC_DRAW)
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 8 * 4, ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 8 * 4, ctypes.c_void_p(3 * 4))
        glEnableVertexAttribArray(1)
        glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, 8 * 4, ctypes.c_void_p(6 * 4))
        glEnableVertexAttribArray(2)
        self.main_shader = create_shader(vertex_shader1, fragment_shader1)

    def _draw_texture(self, texture):
        glBindTexture(GL_TEXTURE_2D, texture)
        glUseProgram(self.main_shader)
        glBindVertexArray(self.VAO)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def close(self):
        glDeleteVertexArrays(1, self.VAO)
        glDeleteBuffers(1, self.VBO)
        glDeleteBuffers(1, self.EBO)
        glDeleteTextures(1, self.texture)
        glDeleteProgram(self.main_shader)


