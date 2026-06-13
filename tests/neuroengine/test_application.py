#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2026-04-23 10:57
import pytest
from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder


class TestApplication:

    def test_application(self):
        # NeuroEngine is now a pure orchestrator — use NeuroEngineBuilder to assemble it.
        self.app = NeuroEngineBuilder().build()
        self.app.run()

    def teardown(self):
        pass


if __name__ == '__main__':
    pytest.main(['-s', '-v', './tests/neuroengine/test_application.py'])
