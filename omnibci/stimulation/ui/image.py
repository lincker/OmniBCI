#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2023-09-04 09:51
import numpy as np
from PIL import Image
from OpenGL.GL import *
from OpenGL.GL.shaders import compileShader, compileProgram



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
    def __init__(self, image_path: str = None, image: Image = None, window_size=(1920, 1080), position=(0, 0)):
        self.window_width, self.window_height = window_size
        self.pos_x, self.pos_y = position   #图片坐标，像素单位
        if image:
            self.image = image
        else:
            self.image = self._load_image(path=image_path)
        # 获取图片实际尺寸
        self.image_width = self.image.size[0]
        self.image_height = self.image.size[1]
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
        # 计算归一化坐标（使原点在窗口中心，范围[-1, 1]）
        # 将像素坐标转换为OpenGL的标准化设备坐标
        half_width = self.image_width / 2
        half_height = self.image_height / 2

        # 计算位置偏移（归一化到[-1, 1]）
        pos_x_ndc = (self.pos_x / self.window_width) * 2
        pos_y_ndc = (self.pos_y / self.window_height) * 2

        # 计算尺寸缩放（归一化到[-1, 1]）
        scale_x = (self.image_width / self.window_width)
        scale_y = (self.image_height / self.window_height)

        # 顶点数据：位置(x,y,z), 颜色(r,g,b), 纹理坐标(s,t)
        # 使用计算出的实际位置和尺寸
        self.vertices = np.array([
            # 右上角
            pos_x_ndc + scale_x, pos_y_ndc + scale_y, 0.0,  # 位置
            1.0, 0.0, 0.0,  # 颜色（红色）
            1.0, 1.0,  # 纹理坐标

            # 右下角
            pos_x_ndc + scale_x, pos_y_ndc - scale_y, 0.0,  # 位置
            0.0, 1.0, 0.0,  # 颜色（绿色）
            1.0, 0.0,  # 纹理坐标

            # 左下角
            pos_x_ndc - scale_x, pos_y_ndc - scale_y, 0.0,  # 位置
            0.0, 0.0, 1.0,  # 颜色（蓝色）
            0.0, 0.0,  # 纹理坐标

            # 左上角
            pos_x_ndc - scale_x, pos_y_ndc + scale_y, 0.0,  # 位置
            1.0, 1.0, 0.0,  # 颜色（黄色）
            0.0, 1.0  # 纹理坐标
        ], dtype=np.float32)
        # self.vertices = np.array([
        #     1.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0,
        #     1.0, -1.0, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0,
        #     -1.0, -1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
        #     -1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0
        # ], dtype=np.float32)
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
        # 解绑
        glBindBuffer(GL_ARRAY_BUFFER, 0)
        glBindVertexArray(0)

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


# 当渲染程序能够运行时，采用帧缓冲，与渲染缓冲加速。https://learnopengl-cn.github.io/04%20Advanced%20OpenGL/05%20Framebuffers/
class RectImage:
    def __init__(self, image = None, image_path: str = None, window_size=(1920, 1080)):
        self.window_width, self.window_height = window_size
        if image:
            self.image = image
        else:
            self.image = self._load_image(image_path)

        self.load_texture(self.image)

        self.setup_geometry()

    def _load_image(self, image_path):
        try:
            image = Image.open(image_path)
        except FileNotFoundError:
            raise FileNotFoundError(image_path)
        if image.mode != 'RGBA':
            image = image.convert('RGBA')
        # # 优化：调整纹理尺寸为2的幂（提高性能）TODO: check and refactor in future
        # new_width = self.next_power_of_two(width)
        # new_height = self.next_power_of_two(height)
        #
        # if new_width != width or new_height != height:
        #     img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        #     width, height = new_width, new_height
        return image

    def load_texture(self, image):
        """加载 PNG 纹理"""
        img = image

        img_data = np.array(list(img.getdata()), np.uint8)

        self.texture_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.texture_id)

        # 设置纹理参数
        # 当纹理坐标超出【0，1】时，将小于0和大于1的坐标分别设置为0，1
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        # 多级渐远纹理功能  禁用mipmaps，GL_NEAREST最快
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)

        # 上传纹理数据
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, img.width, img.height,
                     0, GL_RGBA, GL_UNSIGNED_BYTE, img_data)

        glBindTexture(GL_TEXTURE_2D, 0) #解绑

        self.img_width = img.width
        self.img_height = img.height

    def setup_shaders(self):
        """设置着色器程序"""
        vertex_shader_source = """
                #version 330 core
                layout (location = 0) in vec3 aPos;
                layout (location = 1) in vec2 aTexCoord;
                out vec2 TexCoord;

                void main()
                {
                    gl_Position = vec4(aPos, 1.0);
                    TexCoord = aTexCoord;
                }
                """

        fragment_shader_source = """
                #version 330 core
                in vec2 TexCoord;
                out vec4 FragColor;
                uniform sampler2D texture1;

                void main()
                {
                    FragColor = texture(texture1, TexCoord);
                }
                """

        # 编译着色器
        vertex_shader = compileShader(vertex_shader_source, GL_VERTEX_SHADER)
        fragment_shader = compileShader(fragment_shader_source, GL_FRAGMENT_SHADER)

        # 创建着色器程序
        self.shader_program = compileProgram(vertex_shader, fragment_shader)

    def setup_geometry(self):
        """设置顶点数据"""
        vertices = np.array([
            # 位置              # 纹理坐标
            -0.5, -0.5, 0.0, 0.0, 0.0,
            0.5, -0.5, 0.0, 1.0, 0.0,
            0.5, 0.5, 0.0, 1.0, 1.0,
            -0.5, 0.5, 0.0, 0.0, 1.0
        ], dtype=np.float32)

        indices = np.array([
            0, 1, 2,
            2, 3, 0
        ], dtype=np.uint32)

        # 创建 VAO, VBO, EBO
        self.VAO = glGenVertexArrays(1)
        self.VBO = glGenBuffers(1)
        self.EBO = glGenBuffers(1)

        glBindVertexArray(self.VAO)

        # 填充 VBO
        glBindBuffer(GL_ARRAY_BUFFER, self.VBO)
        glBufferData(GL_ARRAY_BUFFER, vertices.nbytes, vertices, GL_STATIC_DRAW)

        # 填充 EBO
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, self.EBO)
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL_STATIC_DRAW)

        # 设置顶点属性
        # 位置属性
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 5 * vertices.itemsize,
                              ctypes.c_void_p(0))
        glEnableVertexAttribArray(0)

        # 纹理坐标属性
        glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 5 * vertices.itemsize,
                              ctypes.c_void_p(3 * vertices.itemsize))
        glEnableVertexAttribArray(1)
        self.setup_shaders()
        glBindBuffer(GL_ARRAY_BUFFER, 0)    #解绑VBO
        glBindVertexArray(0)    #解绑VAO

    def draw(self):
        glBindTexture(GL_TEXTURE_2D, self.texture_id)
        glUseProgram(self.shader_program)
        glBindVertexArray(self.VAO)
        glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)

    def close(self):
        glDeleteVertexArrays(1, self.VAO)
        glDeleteBuffers(1, self.VBO)
        glDeleteBuffers(1, self.EBO)
        glDeleteTextures(1, self.texture_id)
        glDeleteProgram(self.shader_program)