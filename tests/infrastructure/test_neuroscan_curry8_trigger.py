"""CURRY 8 TTL output through a simulated InpOut driver; no hardware required."""

import ctypes
import sys
from unittest.mock import Mock, call

import numpy as np
from tests.plugin_configuration_helpers import builtin_selection

import pytest

from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8 import trigger
from omnibci.application.configuration.models import ProfileBinding
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.infrastructure.stimulation.stimulator import Stimulator


def prepared(*, parameters):
    output = trigger.Curry8Trigger(parameters=parameters)
    output.setup()
    output.clear()
    return output


@pytest.fixture
def driver(monkeypatch):
    library = Mock(spec=["Out32", "IsInpOutDriverOpen"])
    library.IsInpOutDriverOpen.return_value = True
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=library), raising=False)
    monkeypatch.setattr(trigger.time, "sleep", Mock())
    return library


@pytest.mark.parametrize("pointer_size,dll", [(4, "inpout32.dll"), (8, "inpoutx64.dll")])
def test_initialization_uses_process_bitness_and_resets_output(driver, monkeypatch, pointer_size, dll):
    monkeypatch.setattr(ctypes, "sizeof", lambda _: pointer_size)
    output = prepared(parameters={"parallel_port_address": "0xD010"})

    ctypes.WinDLL.assert_called_once_with(dll)
    assert output.address == 0xD010
    assert driver.Out32.call_args_list == [call(0xD010, 0)]
    assert driver.Out32.argtypes == [ctypes.c_short, ctypes.c_short]
    assert driver.Out32.restype is None
    trigger.time.sleep.assert_called_once_with(0.02)


@pytest.mark.parametrize("address", ["0xFFF8", "0XFFF8"])
def test_high_address_and_marker_bits_survive_the_ctypes_short_boundary(driver, address):
    writes = []
    callback = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

    @callback(None, ctypes.c_short, ctypes.c_short)
    def write_port(port, value):
        # InpOut preserves the low 16 bits of the configured port address.
        writes.append((port, port & 0xFFFF, value))

    driver.Out32 = write_port
    output = prepared(parameters={"parallel_port_address": address})
    markers = [1, 2, 4, 8, 16, 32, 64, 128, 255]
    for marker in markers:
        output.send(marker)
        output.clear()

    assert writes == [(-8, 0xFFF8, 0)] + [
        (-8, 0xFFF8, value) for marker in markers for value in (marker, 0)
    ]


@pytest.mark.parametrize("address", ["0x3100", "0xFFF8"])
def test_send_clear_sequence_preserves_events_and_repeated_markers(driver, address):
    output = prepared(parameters={"parallel_port_address": address})
    driver.Out32.reset_mock()
    trigger.time.sleep.reset_mock()

    for event in (1, 40, 255, 255, np.int64(2)):
        assert output.send(event) is None
        assert output.clear() is None
    # Zero is a valid baseline for the existing no-cue/free-run paradigm.
    assert output.send(0) is None
    output.clear()
    assert driver.Out32.call_args_list == [
        call(int(address, 16), value) for value in (1, 0, 40, 0, 255, 0, 255, 0, 2, 0, 0, 0)
    ]
    assert trigger.time.sleep.call_args_list == [call(0.02)] * 6


@pytest.mark.parametrize("event", [-1, 256, 65536, 1.5, "1", None, True])
def test_invalid_events_do_not_write_to_hardware(driver, event):
    output = prepared(parameters={})
    driver.Out32.reset_mock()
    with pytest.raises(ValueError, match="0.*255"):
        output.send(event)
    driver.Out32.assert_not_called()


@pytest.mark.parametrize("address,expected", [("0x378", 0x378), ("D010", 0xD010), ("0X3100", 0x3100)])
def test_hex_addresses_are_not_restricted_to_legacy_default(driver, address, expected):
    assert prepared(parameters={"parallel_port_address": address}).address == expected


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_unsupported_platform_does_not_load_or_fake_hardware(driver, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    with pytest.raises(RuntimeError, match="Windows"):
        prepared(parameters={})
    ctypes.WinDLL.assert_not_called()


def test_missing_dll_has_actionable_error(driver):
    ctypes.WinDLL.side_effect = OSError("DLL missing")
    with pytest.raises(RuntimeError, match="[Ii]nstall.*InpOut"):
        prepared(parameters={})
    driver.Out32.assert_not_called()


def test_unavailable_kernel_driver_does_not_write(driver):
    driver.IsInpOutDriverOpen.return_value = False
    with pytest.raises(RuntimeError, match="driver.*not open"):
        prepared(parameters={})
    driver.Out32.assert_not_called()


def test_incompatible_dll_does_not_write(driver):
    del driver.IsInpOutDriverOpen
    with pytest.raises(RuntimeError, match="InpOut"):
        prepared(parameters={})
    driver.Out32.assert_not_called()


@pytest.mark.parametrize("operation", ["initialize", "send", "clear"])
def test_port_write_errors_are_propagated(driver, operation):
    if operation != "initialize":
        output = prepared(parameters={})
    driver.Out32.side_effect = OSError("port unavailable")
    with pytest.raises(OSError, match="port unavailable"):
        if operation == "initialize":
            prepared(parameters={})
        elif operation == "send":
            output.send(3)
        else:
            output.clear()


@pytest.mark.parametrize("address", ["0x378", "0xFFF8"])
def test_stimulator_loads_exact_curry_trigger_and_free_run_keeps_zero(driver, address, monkeypatch):
    catalog = PluginCatalog()
    selected_trigger = catalog.resolve_selection("trigger", builtin_selection("trigger", "neuroscan_curry8_trigger"))
    selected_paradigm = catalog.resolve_selection("paradigm", builtin_selection("paradigm", "STIMULATION_THUBCI_SSVEP_classic40"))
    stimulator = Stimulator(settings_snapshot={"PLUGINS.bindings": [
        ProfileBinding("stimulation.trigger", "trigger", None, selected_trigger.selection,
                       {"parallel_port_address": address}).to_dict(),
        ProfileBinding("stimulation.paradigm", "paradigm", None, selected_paradigm.selection,
                       {}).to_dict(),
    ]})
    output = stimulator._load_trigger()
    assert output.plugin_type == "neuroscan_curry8_trigger"
    output.setup()
    output.clear()
    paradigm = stimulator._load_paradigm()
    # Borrow through the real setup signature; substitute the large frame pack.
    module = sys.modules[type(paradigm).__module__]
    pack = Mock()
    pack.__enter__ = Mock(return_value=pack)
    pack.__exit__ = Mock(return_value=None)
    pack.idle_frame = np.zeros((1, 1, 1), dtype=np.uint8)
    pack.iter_stimulation_frames.return_value = iter(())
    pack.iter_cue_frames.return_value = iter(())
    pack.iter_result_frames.return_value = iter(())
    from contextlib import nullcontext
    monkeypatch.setattr(module, "_frame_pack_path", lambda: nullcontext("fake.ofp"))
    monkeypatch.setattr(module, "FramePack", lambda path: pack)
    paradigm.setup(renderer=Mock(), trigger=output)
    driver.Out32.reset_mock()
    paradigm.trial_stimulus_present(None)
    base = int(address, 16)
    assert driver.Out32.call_args_list == [call(base, 0), call(base, 0)]
    output.send(40)
    output.clear()
    paradigm.teardown()
    output.teardown()
    assert driver.Out32.call_args_list[-2:] == [call(base, 40), call(base, 0)]
