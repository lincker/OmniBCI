#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-09 11:21
import sys
sys.path.append('')


from omnibci.infrastructure.stimulation.creator.factory import UIFactory
from omnibci.infrastructure.stimulation.creator.stim_config import SSVEPConfig

config = SSVEPConfig()
factory = UIFactory(config)
frames = factory.get_frames()
factory.save_frames(frames)
