"""Trigger constructors are inert; setup failures never select no-trigger mode."""
import ctypes
from unittest.mock import Mock

import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.trigger import Curry8Trigger
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45Trigger


@pytest.mark.parametrize('implementation', [Curry8Trigger, Scan45Trigger])
def test_constructor_and_partial_teardown_need_no_device(implementation, monkeypatch):
    monkeypatch.setattr('sys.platform', 'linux')
    output = implementation(parameters={})
    assert output.teardown() is None
    with pytest.raises(RuntimeError, match='Windows'):
        output.setup()
    assert output.teardown() is None


@pytest.mark.parametrize('implementation', [Curry8Trigger, Scan45Trigger])
def test_driver_is_acquired_only_in_setup_and_cannot_send_after_teardown(implementation, monkeypatch):
    driver = Mock()
    driver.IsInpOutDriverOpen.return_value = True
    load = Mock(return_value=driver)
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setattr(ctypes, 'WinDLL', load, raising=False)
    monkeypatch.setattr(Scan45Trigger, '_set_parallel_port', load)
    output = implementation(parameters={})
    load.assert_not_called()
    assert output.setup() is None
    assert load.call_count == 1
    assert output.send(40) is None
    assert output.teardown() is None
    with pytest.raises(RuntimeError, match='setup|initialized'):
        output.send(40)
    assert output.teardown() is None


@pytest.mark.parametrize('event', [-1, 256, True, 1.5, '1'])
def test_scan45_invalid_markers_never_reach_driver(event, monkeypatch):
    driver = Mock()
    monkeypatch.setattr('sys.platform', 'win32')
    monkeypatch.setattr(Scan45Trigger, '_set_parallel_port', lambda *args: driver)
    output = Scan45Trigger(parameters={})
    output.setup()
    driver.setData.reset_mock()
    with pytest.raises(ValueError, match='0.*255'):
        output.send(event)
    driver.setData.assert_not_called()
