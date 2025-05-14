#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-10 22:53
#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @File     : video_stimuli.py
# @Author   : lincker
# @Date     : 2023-05-26 14:47
import time

# from psychopy import visual
import glfw
# from PIL import Image
#pip install PyOpenGL
# from OpenGL.GL import *
import os
# import numpy as np

import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stimulation.ui.image import StimImage
from stimulation.ui.window import UIWindow

from acquisition.device_adapters.neuroscan.scan45.adapter import Scan45Trigger

class SSVEPStimuli:
    def __init__(self):
        self.current_time = glfw.get_time()
        print('current time: ', self.current_time)
        self.time_threshold = (1/60) * 1.5
        self.counter = 0
        self.window = UIWindow('rabbit', 1920, 1080)
        self.trigger = Scan45Trigger()
        # self.frame_num = 480
        self.frame_num = 180
        # image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '0.png')
        # self.image = ImageStim(image_path)


        # init_image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '1_0.jpg')
        # self.init_frame = ImageStim(init_image_path)
        # self.stim_frames = []
        # for i in range(1, self.frame_num):
        #     image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'image_folder', f'0_{i}.jpg')
        #     stim_frame = ImageStim(image_path)
        #     self.stim_frames.append(stim_frame)
        init_image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creator', 'stim', 'pics', 'ssvep', 'initial_frame.png')
        self.init_frame = StimImage(init_image_path)
        self.stim_frames = []
        for i in range(0, self.frame_num):
            image_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creator', 'stim', 'pics', 'ssvep',
                                      f'{i}.png')
            stim_frame = StimImage(image_path)
            self.stim_frames.append(stim_frame)

    def setup(self):
        return

    def teardown(self):
        # self.trigger.teardown()
        print(f'counter = {self.counter}')
        for stim_frame in self.stim_frames:
            stim_frame.close()
        self.window.teardown()

    def play(self):
        # self._drop_frame_counter()
        self.init_frame.draw()
        self.window.flip()
        # send trigger
        # while not self.window.should_close():
        #     glfw.poll_events()
            # self._drop_frame_counter()
            # self.stim_frames.draw()
            # self.window.flip()
        for i in range(5):
            print(f'trail = {i}, counter = {self.counter}')
            time.sleep(1)
            self.current_time = glfw.get_time() # TODO: refactoring
            self.trigger.send(101)
            self.trial_play()
            self.trigger.send(102)

    def display_init_frame(self):
        self.init_frame.draw()
        self.window.flip()

    def display_feedback(self, feedback):
        # self.init_frame.draw()
        self._draw_feedback(index = feedback)
        self.window.flip()
        time.sleep(2)

    def _draw_feedback(self, index):
        # index value is 0-39
        # factory = UIFactory(SSVEPConfig())
        image = self.init_frame.image # Image
        image = image.copy()
        from PIL import ImageDraw
        draw = ImageDraw.Draw(image)
        # draw.rectangle([225,90,365,230], outline=(255,0,0), width=3)
        # todo: set the correct position
        # draw.rectangle([225+50*index,90+50*index,365+50*index,150+50*index], outline=(255,0,0), width=3)
        # the first stim image position: left top (225, 90), right bottom (365, 230)
        # interval is 50
        row = index // 8
        colum = (index - row * 8)
        # colum = index % 5
        x0 = 225 + (50 + 140) * colum
        y0 = 90 + (50 + 140) * row
        x1 = x0 + 140
        y1 = y0 + 140
        draw.rectangle([x0, y0, x1, y1], outline=(255,0,0), width=3)
        feedback_image = StimImage(image=image)
        feedback_image.draw()
        print('draw feedback')

    def trial_play(self):
        self.trigger.send(101)
        for stim_frame in self.stim_frames:
            self._drop_frame_counter()
            stim_frame.draw()
            self.window.flip()
            self.window.poll_events()
        self.trigger.send(102)

    def _drop_frame_counter(self):
        ctime = glfw.get_time()
        interval = ctime - self.current_time
        self.counter += 1
        # print(' time interval: ', interval)
        if interval > self.time_threshold:
            print(f'counter = {self.counter}, drop frame current time: {ctime},  interval : {interval}')
        self.current_time = ctime





