"""Discovery and loading contracts for the packaged Neuroscan drivers."""

import ctypes
import sys
from importlib import import_module
from unittest.mock import Mock, call

import pytest

from omnibci.conf import OmniBCISettings
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.plugins.loader import PluginLoader
from omnibci.sdk import PluginContext
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog, list_plugins


@pytest.mark.parametrize(
    ("driver", "class_name"),
    [("scan45", "Scan45DataReader"), ("curry8", "Curry8DataReader")],
)
def test_neuroscan_driver_is_discoverable_and_loadable(
    monkeypatch, tmp_path, driver, class_name,
):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    plugin_type = f"neuroscan_{driver}_reader"

    plugins = {plugin.plugin_type: plugin for plugin in list_plugins("acquisition")}
    descriptor = PluginCatalog().resolve("acquisition", plugin_type)
    instance = PluginLoader.instantiate(descriptor, SignalSource, parameters={}, context=PluginContext())

    assert plugins[plugin_type].source == "builtin"
    assert descriptor.location.parts[-2:] == ("neuroscan", driver)
    assert str(descriptor.entrypoint) == f"{driver}:{class_name}"
    assert type(instance).__name__ == class_name
    assert instance.plugin_type == plugin_type
    assert not instance.is_connected


@pytest.mark.parametrize(
    ("configured_address", "explicit_address", "expected_address"),
    [
        ("0x378", None, 0x378),
        ("0378", None, 0x378),
        ("3100", None, 0x3100),
        ("0x3100", "3BC", 0x3BC),
    ],
)
def test_scan45_trigger_uses_configured_address_and_sends_events(
    monkeypatch, tmp_path, configured_address, explicit_address, expected_address,
):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve("acquisition", "neuroscan_scan45_reader")
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    support_module = import_module(".._inpout", driver_module.__name__)
    port_class = Mock()
    monkeypatch.setattr(
        driver_module, "settings",
        {"ACQUISITION.parallel_port_address": configured_address},
    )
    monkeypatch.setattr(support_module, "PParallelInpOut", port_class)
    monkeypatch.setattr(ctypes, "windll", Mock(), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    trigger = driver_module.Scan45Trigger(parallel_port_address=explicit_address)

    trigger.send(7)
    trigger.clear()

    port_class.assert_called_once_with(expected_address)
    assert trigger.address == expected_address
    assert trigger.port is port_class.return_value
    assert trigger.port.setData.call_args_list == [call(0), call(7), call(0)]


def test_scan45_trigger_rejects_missing_address_before_opening_port(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve("acquisition", "neuroscan_scan45_reader")
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    config_path = tmp_path / "settings.toml"
    config_path.write_text("[ACQUISITION]\n", encoding="utf-8")
    monkeypatch.setattr(driver_module, "settings", OmniBCISettings(config_path))
    open_port = Mock()
    monkeypatch.setattr(driver_module.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(driver_module.Scan45Trigger, "clear", Mock())

    with pytest.raises(
        NameError, match=r"settings key not found: ACQUISITION\.parallel_port_address",
    ):
        driver_module.Scan45Trigger()

    open_port.assert_not_called()


@pytest.mark.parametrize("address", ["", "COM3", "0xinvalid"])
def test_scan45_trigger_rejects_invalid_address_before_opening_port(
    monkeypatch, tmp_path, address,
):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve("acquisition", "neuroscan_scan45_reader")
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    monkeypatch.setattr(
        driver_module, "settings", {"ACQUISITION.parallel_port_address": address}
    )
    open_port = Mock()
    monkeypatch.setattr(driver_module.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(driver_module.Scan45Trigger, "clear", Mock())

    with pytest.raises(ValueError):
        driver_module.Scan45Trigger()

    open_port.assert_not_called()


def test_scan45_trigger_uses_installed_worker_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve("acquisition", "neuroscan_scan45_reader")
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    config_path = tmp_path / "settings.toml"
    config_path.write_text(
        '[ACQUISITION]\nparallel_port_address = "0x3100"\n', encoding="utf-8"
    )
    settings = OmniBCISettings(config_path)
    settings.use_snapshot({"ACQUISITION.parallel_port_address": "0x378"})
    monkeypatch.setattr(driver_module, "settings", settings)
    open_port = Mock()
    monkeypatch.setattr(driver_module.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(driver_module.Scan45Trigger, "clear", Mock())

    trigger = driver_module.Scan45Trigger()

    assert trigger.address == 0x378
    open_port.assert_called_once_with(0x378)
