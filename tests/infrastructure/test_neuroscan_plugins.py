"""Discovery and loading contracts for the packaged Neuroscan drivers."""

import ctypes
import sys
from importlib import import_module
from unittest.mock import Mock

import pytest

from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.plugins.loader import PluginLoader
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
    instance = PluginLoader.instantiate(descriptor, SignalSource)

    assert plugins[plugin_type].source == "builtin"
    assert descriptor.location.parts[-2:] == ("neuroscan", driver)
    assert str(descriptor.entrypoint) == f"{driver}:{class_name}"
    assert type(instance).__name__ == class_name
    assert instance.plugin_type == plugin_type
    assert not instance.is_connected


def test_scan45_trigger_loads_its_packaged_parallel_port_driver(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(tmp_path / "no-user-plugins"))
    descriptor = PluginCatalog().resolve("acquisition", "neuroscan_scan45_reader")
    reader_class = PluginLoader.load_entrypoint(descriptor, SignalSource)
    driver_module = sys.modules[reader_class.__module__]
    support_module = import_module(".._inpout", driver_module.__name__)
    port_class = Mock()
    monkeypatch.setattr(support_module, "PParallelInpOut", port_class)
    monkeypatch.setattr(ctypes, "windll", Mock(), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    trigger = driver_module.Scan45Trigger.__new__(driver_module.Scan45Trigger)

    port = trigger._set_parallel_port(0x3100)

    port_class.assert_called_once_with(0x3100)
    assert port is port_class.return_value
