'''
Author: lincker
Date: 2025-09-22 10:46:51
LastEditors: lincker haolin.d.wu@gmail.com
LastEditTime: 2025-12-25 11:08:25
FilePath: /OmniBCI/omnibci/plugins/algorithms/ssvep/classic40CCA.py
Description:

Copyright (c) 2025 by lincker, All Rights Reserved.
'''
import math
from typing import Optional

#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-09-22 10:46

import numpy as np
from numpy import typing as npt
from sklearn.cross_decomposition import CCA as skCCA

from omnibci.conf import settings
from omnibci.domain.ports.algorithm import Decoder, Prediction
from omnibci.sdk import PluginContext, PluginParameters


class Classic40CCA(Decoder):
    plugin_type = 'THUBCI_SSVEP_classic40CCA'
    class Template:
        # 倍频数
        MULTIPLY_TIME = 5

        def __init__(self, sample_rate, sample_count) -> None:
            self.SAMPLE_RATE = sample_rate
            self.SAMPLE_COUNT = sample_count
            self.FREQUENCY_SET = [  8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0,
                                    8.2, 9.2, 10.2, 11.2, 12.2, 13.2, 14.2, 15.2,
                                    8.4, 9.4, 10.4, 11.4, 12.4, 13.4, 14.4, 15.4,
                                    8.6, 9.6, 10.6, 11.6, 12.6, 13.6, 14.6, 15.6,
                                    8.8, 9.8, 10.8, 11.8, 12.8, 13.8, 14.8, 15.8]

        def generate_template(self) -> [npt.NDArray]:
            template_set = []
            sample_points = np.linspace(0, (self.SAMPLE_COUNT - 1) / self.SAMPLE_RATE, int(self.SAMPLE_COUNT),
                                        endpoint=True)
            sample_points = sample_points.reshape(1, len(sample_points))
            for index in range(0, len(self.FREQUENCY_SET)):
                frequency = self.FREQUENCY_SET[index]
                test_frequency = np.linspace(frequency, frequency * self.MULTIPLY_TIME, int(self.MULTIPLY_TIME),
                                             endpoint=True)
                test_frequency = test_frequency.reshape(1, len(test_frequency))
                num_matrix = 2 * np.pi * np.dot(test_frequency.T, sample_points)
                cos_set = np.cos(num_matrix)
                sin_set = np.sin(num_matrix)
                cos_sin_set = np.append(cos_set, sin_set, axis=0)
                template_set.append(cos_sin_set)
            return template_set

    def __init__(
        self, *, parameters: PluginParameters | None = None,
        context: PluginContext | None = None,
    ) -> None:
        super().__init__()
        if parameters is None and context is None:
            self.SAMPLE_RATE = settings.get(
                'ACQUISITION.sample_rate', default=settings.get('ssvep.sample_rate', default=1000))
            window_duration = settings.get('ssvep.duration', default=3)
            offset_time = settings.get('ssvep.offset_time', default=0.14)
        else:
            if context is None or context.sample_rate is None:
                raise ValueError("Classic40CCA requires context.sample_rate")
            parameters = parameters if parameters is not None else {}
            self.SAMPLE_RATE = context.sample_rate
            window_duration = parameters.get('window_duration', 3.0)
            offset_time = parameters.get('offset_time', 0.14)
        self.SAMPLE_COUNT = int(window_duration * self.SAMPLE_RATE)
        if self.SAMPLE_COUNT < 2:
            raise ValueError("Decode window must contain at least two samples")
        self.OFFSET_LENGTH = math.floor(offset_time * self.SAMPLE_RATE)
        self.template_set = self.Template(sample_rate=self.SAMPLE_RATE,
                                          sample_count=self.SAMPLE_COUNT).generate_template()
        self.cca = skCCA(n_components=1)

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
        return Prediction(target_id=int(self.predict(calc_data)))

    def predict(self, data):
        p = []
        data_t = data.T
        for frequencyIndex in range(0, len(self.template_set)):
            template = self.template_set[frequencyIndex]
            # 确保模板与数据时间点对齐
            template_t = template[:, :data.shape[1]].T  # 截断并转置
            self.cca.fit(data_t, template_t)
            S_d, S_t = self.cca.transform(data_t, template_t)

            r = np.corrcoef(S_d[:, 0], S_t[:, 0])[0, 1]
            p.append(r)
        result = p.index(max(p))
        template_set_index = result + 1
        return template_set_index
