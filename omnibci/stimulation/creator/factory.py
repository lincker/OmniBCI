#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-08 15:59
import numpy as np
import matplotlib.patches as patches

from ssvep_target_rect import SSVEPRectView
import matplotlib.pyplot as plt
import os
import pickle
import matplotlib
import shutil
from tqdm import tqdm

matplotlib.use('agg')


class Frameloader:
    def __init__(self) -> None:
        self.init_frame = None
        self.frame_set = None
        self.stim_rect_set = None


def fig2data(fig):
    """
    fig = plt.figure()
    image = fig2data(fig)
    @brief Convert a Matplotlib figure to a 4D numpy array with RGBA channels and return it
    @param fig a matplotlib figure
    @return a numpy 3D array of RGBA values
    """
    import PIL.Image as Image
    # draw the renderer
    fig.canvas.draw()

    # Get the RGBA buffer from the figure
    w, h = fig.canvas.get_width_height()
    buf = np.fromstring(fig.canvas.tostring_argb(), dtype=np.uint8)
    buf.shape = (w, h, 4)

    # canvas.tostring_argb give pixmap in ARGB mode. Roll the ALPHA channel to have it in RGBA mode
    buf = np.roll(buf, 3, axis=2)
    image = Image.frombytes("RGBA", (w, h), buf.tostring())
    image = np.asarray(image)
    return image


class UIFactory:

    def __init__(self, config):

        self.row_count, self.column_count = config.layout
        self.x_resolution, self.y_resolution = config.resolution
        self.refresh_rate = config.refresh_rate
        self.stim_time = config.stim_time

        self.MAX_FRAMES = int(self.refresh_rate * self.stim_time)

        self.frequency = config.frequency
        self.phase = config.phase
        self.char_set = config.displayed_char

        self.target_width = config.target_width
        self.target_height = config.target_height
        self.interval = config.interval
        self.init_target_x = config.init_target_x
        self.init_target_y = config.init_target_y
        self.paradigm = config.paradigm

        self.save_folder = os.path.join(
            os.getcwd(), 'stim', 'pics', self.paradigm)
        if os.path.exists(self.save_folder) is True:
            shutil.rmtree(self.save_folder)
            os.makedirs(self.save_folder)
        else:
            os.makedirs(self.save_folder)

    def save_frames(self, frames):

        frameSet = frames.frame_set
        for i, frame in enumerate(frameSet):
            plt.imsave(self.save_folder + os.sep + '%i.png' % i, frame)

        initFrame = frames.init_frame
        plt.imsave(self.save_folder + os.sep + 'initial_frame.png', initFrame)

        rectSet = frames.stim_rect_set
        with open(self.save_folder + os.sep + 'STI.pickle', "wb+") as fp:
            pickle.dump(rectSet, fp, protocol=pickle.HIGHEST_PROTOCOL)

    # def _draw_frame(self, frame, rectSet, charSet):
    #     plt.xlim(0, 1)
    #     plt.ylim(0, 1)
    #     plt.gca().xaxis.set_major_locator(plt.NullLocator())
    #     plt.gca().yaxis.set_major_locator(plt.NullLocator())
    #     plt.subplots_adjust(top=1, bottom=0, left=0,
    #                         right=1, hspace=0, wspace=0)
    #
    #     plt.rcParams['axes.unicode_minus'] = False

    def _draw_patch_text(self, x_loc, y_loc, x_size, y_size, char, brightness, color='b'):
        # 画一个正方形
        target = patches.Rectangle((x_loc, y_loc),
                                   x_size, y_size,
                                   linewidth=1, facecolor=[brightness, brightness, brightness])
        # 写上字
        v = plt.text(x_loc + x_size / 2,
                     y_loc + y_size / 2, char,
                     fontsize=30, horizontalalignment='center', verticalalignment='center', color=color)
        return target

    def _set_stim_targets(self):
        frequency_set = np.array(self.frequency).reshape((self.row_count, self.column_count))
        phase_set = np.array(self.phase).reshape((self.row_count, self.column_count))
        target_set = []
        for row_index, (frequency_row, phase_row) in enumerate(zip(frequency_set, phase_set)):
            for col_index, (frequency, phase) in enumerate(zip(frequency_row, phase_row)):
                target_x = col_index * (self.target_width + self.interval) + self.init_target_x
                target_y = ((self.row_count - row_index - 1) * (self.target_height + self.interval) + self.init_target_y)
                target_set.append(
                    SSVEPRectView(self.target_width, self.target_height, target_x, target_y, self.refresh_rate, frequency, phase))

        # for col_inx, (freq_col, phase_col) in enumerate(zip(frequency_set, phase_set)):
        #     for row_inx, (frequency, phase) in enumerate(zip(freq_col, phase_col)):
        #         target_x = col_inx * (self.target_width + self.interval) + self.init_target_x
        #         target_y = ((self.row_count - row_inx - 1) * (self.target_height + self.interval) + self.init_target_y)
        #         rect_set.append(
        #             SSVEPRectView(self.target_width, self.target_height, target_x, target_y, self.refresh_rate, frequency, phase))
        return target_set


    def get_frames(self):
        target_set = self._set_stim_targets()
        frame_set = []
        for N in tqdm(range(self.MAX_FRAMES + 1)):
            # 从此循环进入每一帧
            f = self._construct_frame(N, target_set)


            plt.axis('off')
            frame_set.append(fig2data(f))
            plt.close(f)

        frames = Frameloader()
        frames.init_frame = frame_set.pop(0)
        frames.frame_set = frame_set
        frames.stim_rect_set = target_set

        return frames

    def _construct_frame(self, frame_num, target_set):
        f = plt.figure(figsize=(19.20, 10.80), facecolor='none', dpi=100)
        plt.xlim(0, 1)
        plt.ylim(0, 1)
        plt.gca().xaxis.set_major_locator(plt.NullLocator())
        plt.gca().yaxis.set_major_locator(plt.NullLocator())
        plt.subplots_adjust(top=1, bottom=0, left=0,
                            right=1, hspace=0, wspace=0)

        plt.rcParams['axes.unicode_minus'] = False  # 这两行需要手动设置
        current_axis = plt.gca()

        char_set = self.char_set
        for target, char in zip(target_set, char_set):
            # 每一帧的每一个目标
            if frame_num == 0:
                brightness = 1
            else:
                brightness = target.cal_brightness(frame_num)
            x_loc = target.position_x / self.x_resolution
            y_loc = target.position_y / self.y_resolution
            x_size = target.width / self.x_resolution
            y_size = target.height / self.y_resolution

            patch = self._draw_patch_text(x_loc, y_loc, x_size, y_size, char, brightness)
            current_axis.add_patch(patch)
        return f