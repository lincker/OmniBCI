#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-11-12 10:47
import math
from typing import Optional

import numpy as np
from numpy import typing as npt
from scipy.signal import cheby1, filtfilt

from omnibci.domain.ports.algorithm import Decoder, Prediction
from omnibci.infrastructure.plugins.registry import register_plugin
from omnibci.conf import settings


@register_plugin(Decoder)
class Classic40FBCCA(Decoder):
    plugin_type = 'THUBCI_SSVEP_classic40FBCCA'
    # class Template:
    #     MULTIPLY_TIME = 5
    #     def __init__(self, sample_rate, sample_count) -> None:
    #         self.SAMPLE_RATE = sample_rate
    #         self.SAMPLE_COUNT = sample_count
    #         self.FREQUENCY_SET = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0,
    #                               8.2, 9.2, 10.2, 11.2, 12.2, 13.2, 14.2, 15.2,
    #                               8.4, 9.4, 10.4, 11.4, 12.4, 13.4, 14.4, 15.4,
    #                               8.6, 9.6, 10.6, 11.6, 12.6, 13.6, 14.6, 15.6,
    #                               8.8, 9.8, 10.8, 11.8, 12.8, 13.8, 14.8, 15.8]
    #
    #     def generate_template(self):
    #         template_set = []
    #         sample_points = np.linspace(0, (self.SAMPLE_COUNT - 1) / self.SAMPLE_RATE, int(self.SAMPLE_COUNT),
    #                                     endpoint=True)
    #         sample_points = sample_points.reshape(1, len(sample_points))
    #         for index in range(0, len(self.FREQUENCY_SET)):
    #             frequency = self.FREQUENCY_SET[index]
    #             test_frequency = np.linspace(frequency, frequency * self.MULTIPLY_TIME, int(self.MULTIPLY_TIME),
    #                                          endpoint=True)
    #             test_frequency = test_frequency.reshape(1, len(test_frequency))
    #             num_matrix = 2 * np.pi * np.dot(test_frequency.T, sample_points)
    #             cos_set = np.cos(num_matrix)
    #             sin_set = np.sin(num_matrix)
    #             cos_sin_set = np.append(cos_set, sin_set, axis=0)
    #             template_set.append(cos_sin_set)
    #         return template_set

    def __init__(self) -> None:
        super().__init__()
        self.FREQUENCY_SET = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0,
                              8.2, 9.2, 10.2, 11.2, 12.2, 13.2, 14.2, 15.2,
                              8.4, 9.4, 10.4, 11.4, 12.4, 13.4, 14.4, 15.4,
                              8.6, 9.6, 10.6, 11.6, 12.6, 13.6, 14.6, 15.6,
                              8.8, 9.8, 10.8, 11.8, 12.8, 13.8, 14.8, 15.8]
        self.SAMPLE_RATE = settings.get('ACQUISITION.sample_rate', default=1000)
        self.SAMPLE_COUNT = int(settings.get('ALGORITHM.window_duration', default=3) * self.SAMPLE_RATE)
        self.OFFSET_LENGTH = math.floor(settings.get('ALGORITHM.offset_time', default=0.14) * self.SAMPLE_RATE)
        # self.template_set = self.Template(sample_rate=self.SAMPLE_RATE,
        #                                   sample_count=self.SAMPLE_COUNT).generate_template()
        self.num_harmonics = 5
        self.num_bands = 3
        # FBCCA 权重系数: w_n = n^(-a) + b, 论文常用值 a=1.25, b=0.25
        a = 1.25
        b = 0.25
        # 预计算权重
        self.weights = np.array([(n + 1) ** (-a) + b for n in range(self.num_bands)])

    def get_reference_signals(self, data_len, target_freq):
        """生成正弦/余弦参考信号"""
        t = np.arange(data_len) / self.SAMPLE_RATE
        reference_signals = []
        for i in range(1, self.num_harmonics + 1):
            reference_signals.append(np.sin(2 * np.pi * i * target_freq * t))
            reference_signals.append(np.cos(2 * np.pi * i * target_freq * t))
        return np.array(reference_signals)  # Shape: (2*harmonics, samples)

    def get_filter_bank(self):
        """
        生成滤波器组系数
        标准 FBCCA 通常设置通带为:
        Band 1: 8-88 Hz
        Band 2: 16-88 Hz
        Band 3: 24-88 Hz
        ...
        """
        nyq = 0.5 * self.SAMPLE_RATE
        filters = []

        # 高频截止通常设为 Nyquist 或一个较高的值(如 88Hz/90Hz)
        high_cut = min(88, nyq - 1)

        for i in range(self.num_bands):
            low_cut = 8 * (i + 1)  # 8Hz, 16Hz, 24Hz...
            if low_cut >= high_cut:
                break

            # 使用 Chebyshev I 型滤波器 (阶数和纹波可根据需要调整)
            b, a = cheby1(N=4, rp=0.5, Wn=[low_cut / nyq, high_cut / nyq], btype='bandpass')
            filters.append((b, a))

        return filters

    def cca_coef(self, X, Y):
        """
        快速计算 CCA 相关系数 (使用 QR 分解加速)
        X: EEG data (Channels, Samples)
        Y: Reference signals (2*Harmonics, Samples)
        """
        # 转置为 (Samples, Features)
        X = X.T
        Y = Y.T

        # 中心化
        X = X - X.mean(axis=0)
        Y = Y - Y.mean(axis=0)

        # QR 分解
        Q_x, R_x = np.linalg.qr(X, mode='reduced')
        Q_y, R_y = np.linalg.qr(Y, mode='reduced')

        # SVD of Q_x.T @ Q_y
        U, S, V = np.linalg.svd(Q_x.T @ Q_y)

        # 返回最大的典型相关系数
        return S[0]

    def should_decode(self, window: npt.NDArray) -> bool:
        return window.shape[1] > self.SAMPLE_COUNT + self.OFFSET_LENGTH

    def decode(
        self,
        window: npt.NDArray,
        latest: npt.NDArray,
    ) -> Optional[Prediction]:
        calc_data = window[
            :,
            self.OFFSET_LENGTH:int(self.SAMPLE_COUNT + self.OFFSET_LENGTH),
        ]
        predict_index, _, _ = self.classify(calc_data, self.FREQUENCY_SET)
        return Prediction(target_id=int(predict_index))

    def classify(self, eeg_data, freq_list):
        """
        执行 FBCCA 分类
        :param eeg_data: 输入脑电数据, Shape (n_channels, n_samples)
        :param freq_list: 候选频率列表
        :return: 预测的频率索引, 预测频率值, 各频率的得分
        """
        n_samples = eeg_data.shape[1]
        filters = self.get_filter_bank()

        # 存储每个频率的最终得分
        scores = np.zeros(len(freq_list))

        # 1. 对 EEG 数据进行滤波器组分解
        filtered_data = []
        for b, a in filters:
            # filtfilt 防止相位偏移
            filtered_data.append(filtfilt(b, a, eeg_data, axis=1))

        # 2. 遍历每一个目标频率
        for freq_idx, freq in enumerate(freq_list):
            # 生成该频率的参考信号
            Y = self.get_reference_signals(n_samples, freq)

            rho_sum = 0

            # 3. 遍历每一个子带
            for band_idx in range(len(filters)):
                X_sb = filtered_data[band_idx]

                # 计算 CCA 相关系数
                rho = self.cca_coef(X_sb, Y)

                # 加权求和: sum(w_n * rho_n^2)
                rho_sum += self.weights[band_idx] * (rho ** 2)

            scores[freq_idx] = rho_sum

        # 找到最大得分对应的索引
        result = np.argmax(scores)
        pred_idx = result + 1
        return pred_idx, freq_list[result], scores
