#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-04-23 10:57
import pytest
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder


class TestApplication:

    @pytest.mark.skip(
        reason="手动冒烟入口，不是自动化测试：构建真实引擎并 run()，"
               "无停止条件（should_stop 永不置位），在 pytest 里会无限挂起，"
               "且会 spawn 真实子进程、绑定真实端口。"
               "需要冒烟时直接运行本文件，Ctrl-C 结束。"
    )
    def test_application(self):
        # NeuroEngine is now a pure orchestrator — use NeuroEngineBuilder to assemble it.
        self.app = NeuroEngineBuilder().build()
        self.app.run()

    def teardown(self):
        pass


if __name__ == '__main__':
    pytest.main(['-s', '-v', './tests/neuroengine/test_application.py'])
