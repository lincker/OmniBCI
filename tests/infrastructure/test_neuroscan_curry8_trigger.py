"""CURRY 8 TTL output through a simulated InpOut driver; no hardware required."""

import ctypes
import sys
from unittest.mock import Mock, call

import numpy as np
import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8 import trigger
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.stimulation.stimulator import Stimulator


@pytest.fixture
def driver(monkeypatch):
    library = Mock()
    library.IsInpOutDriverOpen.return_value = True
    library.Inp32.return_value = 0xFF
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=library), raising=False)
    monkeypatch.setattr(trigger.time, "sleep", Mock())
    return library


@pytest.mark.parametrize("pointer_size,dll", [(4, "inpout32.dll"), (8, "inpoutx64.dll")])
def test_initialization_uses_process_bitness_and_resets_output(driver, monkeypatch, pointer_size, dll):
    monkeypatch.setattr(ctypes, "sizeof", lambda _: pointer_size)
    output = trigger.Curry8Trigger(parameters={"parallel_port_address": "0xD010"})

    ctypes.WinDLL.assert_called_once_with(dll)
    assert output.address == 0xD010
    assert driver.Out32.call_args_list == [
        call(0xD412, 0x3F),  # Byte mode, retaining unrelated ECP bits.
        call(0xD012, 0xDF),  # Output direction, retaining unrelated control bits.
        call(0xD010, 0),
    ]
    assert driver.Out32.argtypes == [ctypes.c_short, ctypes.c_short]
    assert driver.Out32.restype is None
    trigger.time.sleep.assert_called_once_with(0.02)


def test_send_clear_sequence_preserves_events_and_repeated_markers(driver):
    output = trigger.Curry8Trigger(parameters={})
    driver.Out32.reset_mock()
    trigger.time.sleep.reset_mock()

    for event in (1, 40, 255, 255, np.int64(2)):
        assert output.send(event) is True
        assert output.clear() is None
    # Zero is a valid baseline for the existing no-cue/free-run paradigm.
    assert output.send(0) is True
    output.clear()
    assert driver.Out32.call_args_list == [
        call(0x3100, value) for value in (1, 0, 40, 0, 255, 0, 255, 0, 2, 0, 0, 0)
    ]
    assert trigger.time.sleep.call_args_list == [call(0.02)] * 6


@pytest.mark.parametrize("event", [-1, 256, 65536, 1.5, "1", None, True])
def test_invalid_events_do_not_write_to_hardware(driver, event):
    output = trigger.Curry8Trigger(parameters={})
    driver.Out32.reset_mock()
    with pytest.raises(ValueError, match="0.*255"):
        output.send(event)
    driver.Out32.assert_not_called()


@pytest.mark.parametrize("address", ["", "wrong", "0x0", "-1", "0x10000", "0xffff", None, 888, True])
def test_invalid_addresses_fail_before_loading_hardware(driver, address):
    with pytest.raises(ValueError, match="parallel_port_address"):
        trigger.Curry8Trigger(parameters={"parallel_port_address": address})
    ctypes.WinDLL.assert_not_called()
    driver.Out32.assert_not_called()


@pytest.mark.parametrize("address,expected", [("0x378", 0x378), ("D010", 0xD010), ("0X3100", 0x3100)])
def test_hex_addresses_are_not_restricted_to_legacy_default(driver, address, expected):
    assert trigger.Curry8Trigger(parameters={"parallel_port_address": address}).address == expected


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_unsupported_platform_does_not_load_or_fake_hardware(driver, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(RuntimeError, match="Windows"):
        trigger.Curry8Trigger(parameters={})
    ctypes.WinDLL.assert_not_called()


def test_missing_dll_has_actionable_error(driver):
    ctypes.WinDLL.side_effect = OSError("DLL missing")
    with pytest.raises(RuntimeError, match="[Ii]nstall.*InpOut"):
        trigger.Curry8Trigger(parameters={})
    driver.Out32.assert_not_called()


def test_unavailable_kernel_driver_does_not_write(driver):
    driver.IsInpOutDriverOpen.return_value = False
    with pytest.raises(RuntimeError, match="driver.*not open"):
        trigger.Curry8Trigger(parameters={})
    driver.Out32.assert_not_called()


def test_incompatible_dll_does_not_write(driver):
    del driver.IsInpOutDriverOpen
    with pytest.raises(RuntimeError, match="InpOut"):
        trigger.Curry8Trigger(parameters={})
    driver.Out32.assert_not_called()


@pytest.mark.parametrize("operation", ["initialize", "send", "clear"])
def test_port_write_errors_are_propagated(driver, operation):
    if operation != "initialize":
        output = trigger.Curry8Trigger(parameters={})
    driver.Out32.side_effect = OSError("port unavailable")
    with pytest.raises(OSError, match="port unavailable"):
        if operation == "initialize":
            trigger.Curry8Trigger(parameters={})
        elif operation == "send":
            output.send(3)
        else:
            output.clear()


def test_stimulator_loads_exact_curry_trigger_and_free_run_keeps_zero(driver):
    catalog = PluginCatalog()
    selected_trigger = catalog.resolve("trigger", "neuroscan_curry8_trigger")
    selected_paradigm = catalog.resolve("paradigm", "STIMULATION_THUBCI_SSVEP_classic40")
    stimulator = Stimulator(settings_snapshot={"PLUGINS.bindings": [
        ProfileBinding("stimulation.trigger", "trigger", None, selected_trigger.selection,
                       {"parallel_port_address": "0x378"}).to_dict(),
        ProfileBinding("stimulation.paradigm", "paradigm", None, selected_paradigm.selection,
                       {}).to_dict(),
    ]})
    output = stimulator._load_trigger()
    assert output.plugin_type == "neuroscan_curry8_trigger"
    paradigm = stimulator._load_paradigm(output)
    driver.Out32.reset_mock()
    paradigm.trial_stimulus_present(None)
    assert driver.Out32.call_args_list == [call(0x378, 0), call(0x378, 0)]
    output.send(40)
    stimulator.trigger = output
    stimulator.clear_trigger()
    assert driver.Out32.call_args_list[-2:] == [call(0x378, 40), call(0x378, 0)]
