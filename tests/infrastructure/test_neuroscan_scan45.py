"""Scan45 frame decoding, discovery, and parallel-port trigger lifecycle tests."""
import ctypes
from importlib import import_module
from pathlib import Path
import struct
import sys
from unittest.mock import AsyncMock, Mock, call

import numpy as np
import pytest

from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
from omnibci.infrastructure.plugin_management.loader import PluginLoader
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8.trigger import Curry8Trigger
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45 import scan45
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45.scan45 import Scan45Trigger
from tests.plugin_configuration_helpers import builtin_selection


@pytest.mark.asyncio
async def test_metadata_is_unsupported_text_while_output_description_uses_configuration():
    reader = scan45.Scan45DataReader(parameters={"channel_count": 33, "sample_rate": 500}, resource_root=Path(__file__).parent)
    reader._load_package = AsyncMock()

    reader._is_connected = True
    metadata = await reader.observe()
    assert metadata.native_text == "Device metadata retrieval is not implemented for Scan45."
    descriptor = metadata.signal_info
    assert descriptor.signal_channel_count == 32
    assert descriptor.output_row_count == 33
    assert descriptor.sample_rate == 500
    assert metadata.provenance == "user-declared"
    reader._load_package.assert_not_awaited()


def packet(channels, events):
    samples = []
    expected = []
    for sample_index, event in enumerate(events):
        eeg = [sample_index * 100 + channel for channel in range(1, channels)]
        samples.extend([*eeg, 0xABCD0000 | event])
        expected.append(eeg)
    return struct.pack(f"<{len(samples)}I", *samples), np.array(expected).T


@pytest.mark.parametrize("channels", [2, 9, 10, 33, 65])
def test_decode_uses_configured_frame_width_and_final_event_row(channels):
    reader = scan45.Scan45DataReader(parameters={"channel_count": channels}, resource_root=Path(__file__).parent)
    body, eeg = packet(channels, [0, 7, 7, 0, 9])
    header = scan45.Scan45Message.Header("DATA", 2, 2, len(body))

    decoded = reader._parse_body_data(header, body)

    assert decoded.shape == (channels, 5)
    np.testing.assert_array_equal(decoded[:-1], eeg)
    np.testing.assert_array_equal(decoded[-1], [0, 7, 7, 0, 9])


@pytest.mark.parametrize("channels", [2, 10, 33])
def test_event_samples_are_preserved_across_packets(channels):
    reader = scan45.Scan45DataReader(parameters={"channel_count": channels}, resource_root=Path(__file__).parent)
    first, _ = packet(channels, [7, 7])
    second, _ = packet(channels, [7, 8, 9, 0, 7, 8, 0, 8])

    np.testing.assert_array_equal(reader._unpack_int32_eeg(first)[-1], [7, 7])
    np.testing.assert_array_equal(reader._unpack_int32_eeg(second)[-1], [7, 8, 9, 0, 7, 8, 0, 8])


def test_event_run_preserves_all_nonzero_values():
    reader = scan45.Scan45DataReader(parameters={"channel_count": 3}, resource_root=Path(__file__).parent)
    body, eeg = packet(3, [0, 7, 7, 8, 9, 0, 8])

    decoded = reader._unpack_int32_eeg(body)

    np.testing.assert_array_equal(decoded[:-1], eeg)
    np.testing.assert_array_equal(decoded[-1], [0, 7, 7, 8, 9, 0, 8])


@pytest.mark.parametrize("kwargs", [{"parameters": {}}])
def test_explicit_default_retains_ten_channel_layout(kwargs):
    reader = scan45.Scan45DataReader(**kwargs, resource_root=Path(__file__).parent)
    body, eeg = packet(10, [0, 7])
    decoded = reader._unpack_int32_eeg(body)

    assert reader.channel_count == 10
    np.testing.assert_array_equal(decoded[:-1], eeg)
    np.testing.assert_array_equal(decoded[-1], [0, 7])


@pytest.mark.parametrize("channels", [None, True, False, 0, 1, -1, 3.5, "33"])
def test_invalid_channel_count_rejected_before_connecting(channels):
    with pytest.raises(ValueError, match="channel_count"):
        scan45.Scan45DataReader(parameters={"channel_count": channels}, resource_root=Path(__file__).parent)


@pytest.mark.parametrize("body", [b"", bytes(3), bytes(8), bytes(15), bytes(17)])
def test_malformed_frame_is_rejected(body):
    reader = scan45.Scan45DataReader(parameters={"channel_count": 4}, resource_root=Path(__file__).parent)
    with pytest.raises(ValueError, match="complete samples of 4 channels"):
        reader._unpack_int32_eeg(body)


def test_packet_header_size_must_match_body():
    reader = scan45.Scan45DataReader(parameters={"channel_count": 4}, resource_root=Path(__file__).parent)
    header = scan45.Scan45Message.Header("DATA", 2, 2, 32)
    with pytest.raises(ValueError, match="body length"):
        reader._parse_body_data(header, bytes(16))


@pytest.mark.parametrize("format_code,error", [(1, NotImplementedError), (3, ValueError)])
def test_unsupported_data_format_fails_explicitly(format_code, error):
    reader = scan45.Scan45DataReader(parameters={"channel_count": 4}, resource_root=Path(__file__).parent)
    header = scan45.Scan45Message.Header("DATA", 2, format_code, 16)
    with pytest.raises(error):
        reader._parse_body_data(header, bytes(16))


def test_scan45_reader_explicit_parameters_and_defaults():
    reader = scan45.Scan45DataReader(
        parameters={"DATA_PROVIDER_IP": "192.0.2.8", "DATA_PROVIDER_PORT": 9001, "sample_rate": 500},
        resource_root=Path(__file__).parent,
    )
    default_reader = scan45.Scan45DataReader(parameters={}, resource_root=Path(__file__).parent)

    assert (reader.data_provider_ip, reader.data_provider_port) == ("192.0.2.8", 9001)
    assert reader.reader is None and reader.writer is None
    assert (default_reader.data_provider_ip, default_reader.data_provider_port) == ("127.0.0.1", 4000)
    with pytest.raises(TypeError, match="parameters"):
        scan45.Scan45DataReader()


@pytest.mark.parametrize(
    ("driver", "class_name"),
    [("scan45", "Scan45DataReader"), ("curry8", "Curry8DataReader")],
)
def test_neuroscan_driver_is_discoverable_and_loadable(monkeypatch, tmp_path, driver, class_name):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    selection = builtin_selection("acquisition", f"neuroscan_{driver}_reader")

    plugins = {plugin.selection: plugin for plugin in PluginCatalog().list_contributions("acquisition")}
    descriptor = PluginCatalog().resolve_selection("acquisition", selection)
    instance = PluginLoader.instantiate(descriptor, SignalSource, parameters={})

    assert plugins[selection].source == "builtin"
    assert descriptor.location.parts[-2:] == ("neuroscan", driver)
    assert str(descriptor.entrypoint) == f"{driver}:{class_name}"
    assert type(instance).__name__ == class_name
    assert descriptor.selection == selection
    assert instance.reader is None and instance.writer is None


@pytest.mark.parametrize(
    ("configured_address", "expected_address"),
    [
        ("0x378", 0x378),
        ("0378", 0x378),
        ("3100", 0x3100),
        ("3BC", 0x3BC),
    ],
)
def test_scan45_trigger_uses_configured_address_and_sends_events(
    monkeypatch, tmp_path, configured_address, expected_address,
):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "neuroscan_scan45_reader"))
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    support_module = import_module(".._inpout", driver_module.__name__)
    port_class = Mock()
    monkeypatch.setattr(support_module, "PParallelInpOut", port_class)
    monkeypatch.setattr(ctypes, "windll", Mock(), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    trigger = driver_module.Scan45Trigger(parameters={"parallel_port_address": configured_address}, resource_root=Path(__file__).parent)

    trigger.setup()
    trigger.clear()
    trigger.send(7)
    trigger.clear()

    port_class.assert_called_once_with(expected_address)
    assert trigger.address == expected_address
    assert trigger.port is port_class.return_value
    assert trigger.port.setData.call_args_list == [call(0), call(7), call(0)]


def test_scan45_trigger_rejects_missing_address_before_opening_port(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "neuroscan_scan45_reader"))
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    open_port = Mock()
    monkeypatch.setattr(driver_module.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(driver_module.Scan45Trigger, "clear", Mock())

    with pytest.raises(TypeError, match="parameters"):
        driver_module.Scan45Trigger(resource_root=Path(__file__).parent)

    open_port.assert_not_called()


@pytest.mark.parametrize("address", ["", "COM3", "0xinvalid"])
def test_scan45_trigger_rejects_invalid_address_before_opening_port(monkeypatch, tmp_path, address):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "neuroscan_scan45_reader"))
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    open_port = Mock()
    monkeypatch.setattr(driver_module.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(driver_module.Scan45Trigger, "clear", Mock())

    with pytest.raises(ValueError):
        driver_module.Scan45Trigger(parameters={"parallel_port_address": address}, resource_root=Path(__file__).parent)

    open_port.assert_not_called()


@pytest.mark.parametrize("implementation", [Curry8Trigger, Scan45Trigger])
def test_constructor_and_partial_teardown_need_no_device(implementation, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    output = implementation(parameters={}, resource_root=Path(__file__).parent)
    assert output.teardown() is None
    with pytest.raises(RuntimeError, match="Windows"):
        output.setup()
    assert output.teardown() is None


@pytest.mark.parametrize("implementation", [Curry8Trigger, Scan45Trigger])
def test_driver_is_acquired_only_in_setup_and_cannot_send_after_teardown(implementation, monkeypatch):
    driver = Mock()
    driver.IsInpOutDriverOpen.return_value = True
    load = Mock(return_value=driver)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    monkeypatch.setattr(Scan45Trigger, "_set_parallel_port", load)
    output = implementation(parameters={}, resource_root=Path(__file__).parent)
    load.assert_not_called()
    assert output.setup() is None
    assert load.call_count == 1
    assert output.send(40) is None
    assert output.teardown() is None
    with pytest.raises(RuntimeError, match="setup|initialized"):
        output.send(40)
    assert output.teardown() is None


@pytest.mark.parametrize("event", [-1, 256, True, 1.5, "1"])
def test_scan45_invalid_markers_never_reach_driver(event, monkeypatch):
    driver = Mock()
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(Scan45Trigger, "_set_parallel_port", lambda *args: driver)
    output = Scan45Trigger(parameters={}, resource_root=Path(__file__).parent)
    output.setup()
    driver.setData.reset_mock()
    with pytest.raises(ValueError, match="0.*255"):
        output.send(event)
    driver.setData.assert_not_called()
