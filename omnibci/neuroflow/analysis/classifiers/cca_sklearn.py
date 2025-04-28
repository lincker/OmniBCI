#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-04-25 13:39
import math
import numpy as np
from sklearn.cross_decomposition import CCA as skCCA
from omnibci.core.analysis_method import AnalysisMethod

EpochStartEventSet = set(range(1, 8))

class DataContext:
    def __init__(self, sample_rate:int=0, duration=0, offset_time=0.14, frequency_set=None):
        self.sample_rate = sample_rate
        self.sample_count = int(duration * sample_rate)
        self.frequency_set = frequency_set
        self.offset_length = int(offset_time * sample_rate)


class CCATemplate:
    # 倍频数
    MULTIPLY_TIME = 5

    def __init__(self, data_context):
        self.SAMPLE_RATE = data_context.sample_rate
        self.SAMPLE_COUNT = data_context.sample_count
        self.frequency_set = data_context.frequency_set

    def generate_template(self):
        template = []
        # sample points
        t = np.linspace(0, (self.SAMPLE_COUNT - 1) / self.SAMPLE_RATE, int(self.SAMPLE_COUNT), endpoint=True)
        t = t.reshape(1, len(t))
        # for each frequency
        for fre_index in range(0, len(self.frequency_set)):
            frequency = self.frequency_set[fre_index]
            test_frequency = np.linspace(frequency, frequency * self.MULTIPLY_TIME, int(self.MULTIPLY_TIME), endpoint=True)
            test_frequency = test_frequency.reshape(1, len(test_frequency))
            num_matrix = 2 * np.pi * np.dot(test_frequency.T, t)
            cos_set = np.cos(num_matrix)
            sin_set = np.sin(num_matrix)
            cos_sin_set = np.append(cos_set, sin_set, axis=0)
            template.append(cos_sin_set)
        return template


class CCA(AnalysisMethod):

    def __init__(self):
        super().__init__()
        # self.template_set = None
        # self.selected_channels = None
        srate = 1000
        duration = 2
        frequency_set = [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0]
        # self.selectChannel = [48, 54, 55, 56, 57, 58, 61, 62, 63]
        data_context = DataContext(sample_rate=srate, duration=duration, offset_time=0.64, frequency_set=frequency_set)
        template = CCATemplate(data_context)

        self.template_set = template.generate_template()
        self.cca = skCCA(n_components=1)

        # 倍频数
        # multiplicateTime = 5
        self.SAMPLE_COUNT = duration * srate  # cal time * sample rate
        self.OFFSET_LENGTH = math.floor(0.14 * srate)  # offset time * sample rate

    def is_epoch_head(self, data_package):
        data = data_package.data
        event_data = data[data.shape[0] - 1, :]
        # TODO: use the current classifier method judge whether jump to another state
        for index, event in enumerate(event_data):
            # found event trigger
            if event in EpochStartEventSet:
                started = True
                return started, index, event
        # not found event trigger
        return False, 0, 0

    def recognize(self,data, force_calc=False) -> (bool, int):
        # force calculate the data
        if force_calc:
            if data.shape[1] > self.SAMPLE_COUNT + self.OFFSET_LENGTH:
                calc_data = data[:, self.OFFSET_LENGTH:int(self.SAMPLE_COUNT + self.OFFSET_LENGTH)]
            else:
                calc_data = data
            predict_index = self.predict(calc_data)
            return True, predict_index
        # data length enough, calculate the data; else return False
        if data.shape[1] > self.SAMPLE_COUNT + self.OFFSET_LENGTH:
            calc_data = data[:, self.OFFSET_LENGTH:int(self.SAMPLE_COUNT + self.OFFSET_LENGTH)]
            predict_index = self.predict(calc_data)
            return True, predict_index
        else:
            return False, 0

    def predict(self, data):
        p = []
        for frequencyIndex in range(0, len(self.template_set)):
            template = self.template_set[frequencyIndex]
            # template = template[:, 0:data.shape[0]]
            # template = template.T
            self.cca.fit(data.T, template.T)
            S_d, S_t = self.cca.transform(data.T, template.T)
            r = np.corrcoef(S_d[:, 0], S_t[:, 0])[0, 1]
            p.append(r)
        result = p.index(max(p))
        template_set_index = result + 1
        return template_set_index