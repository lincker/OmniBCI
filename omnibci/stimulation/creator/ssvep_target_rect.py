#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-08 16:00
import math
import numpy as np


class StimTargetRect:
    def __init__(self, rectINX, site_point, rect_size, fs, frequency, start_phase, max_amplitude):
        self.rectINX = rectINX
        self.site_point = site_point
        self.rect_size = rect_size
        self.fs = fs
        self.frequency = frequency
        self.start_phase = start_phase
        self.max_amplitude = max_amplitude
        self.form_flag_matrix = np.ones(rect_size, dtype = 'bool')
        # self.position = self.covert2psycho()

    def cal_brightness(self, frame_no):
        brightness = 0.5 + 0.5 * math.sin( 2 * math.pi * self.frequency /self.fs * frame_no + self.start_phase)
        return brightness

    # def covert2psycho(self):
    #     width = self.rect_size / 2
    #     x, y = self.site_point
    #     x = x - 1920 / 2 + width
    #     y = y - 1080 / 2 + width
    #
    #     return [x, y]


from stimulation.ui.view import View
class SSVEPRectView(View):
    def __init__(self, width, height, position_x, position_y, refresh_rate, frequency, start_phase):
        super().__init__(width, height, position_x, position_y)
        self.refresh_rate = refresh_rate
        self.frequency = frequency
        self.start_phase = start_phase

    def cal_brightness(self, frame_no):
        brightness = 0.5 + 0.5 * math.sin( 2 * math.pi * self.frequency /self.refresh_rate * frame_no + self.start_phase)
        return brightness

