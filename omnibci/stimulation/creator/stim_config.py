#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-08 16:03
import numpy as np
import math
import string
import os

class SSVEPConfig:
    def __init__(self):
        self.layout = (5, 8)
        self.resolution = (1920, 1080)
        self.refresh_rate = 60
        self.stim_time = 3
        self.frequency = np.linspace(8.0, 15.8, 40)
        self.phase = np.tile(np.arange(0, 2, 0.5)*math.pi, 10)
        self.char = list(string.ascii_uppercase) + \
            list(np.arange(10))+['.', ' ', ',', 'Del']
        ordered_char = []
        for i in range(5):
            for j in range(8):
                ordered_char.append(self.char[i * 8 + j])
        self.displayed_char = ordered_char
        self.target_width = 140
        self.target_height =140
        self.interval = 50
        self.init_target_x, self.init_target_y = (225, 90)
        self.paradigm = 'ssvep'
        self.pics_save_path = os.path.join(
            os.getcwd(), 'stim', 'pics', 'ssvep')