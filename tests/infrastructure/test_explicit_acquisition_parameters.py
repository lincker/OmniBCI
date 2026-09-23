"""Explicit acquisition constructors do not consult process-global settings."""

import ctypes
import sys
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from omnibci.conf.paths import resolve_config_path
from omnibci.infrastructure.plugins.acquisition.file import reader as file_module
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8 import curry8
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45 import scan45
from omnibci.sdk import PluginContext


@pytest.fixture(autouse=True)
def forbid_settings_reads(monkeypatch):
    getter = Mock(side_effect=AssertionError("Explicit plugins must not read settings"))
    for module in (file_module, curry8, scan45):
        monkeypatch.setattr(module, "settings", SimpleNamespace(get=getter))
    return getter


def test_file_reader_uses_explicit_path_and_chunk_size(tmp_path):
    path = tmp_path / "subject.cnt"
    parameters = {"data_file_path": str(path), "file_reader_chunk_size": 37}

    reader = file_module.FileReader(parameters=parameters, context=PluginContext())
    parameters["file_reader_chunk_size"] = 99

    assert reader.file_path == str(path)
    assert reader._chunk_size == 37
    assert reader.file_pointer == 0
    assert reader.speed == 1.0
    assert not reader.is_connected


@pytest.mark.parametrize("kwargs", [{"parameters": {}}, {"context": PluginContext()}])
def test_file_reader_explicit_defaults_never_fall_back_to_settings(kwargs):
    reader = file_module.FileReader(**kwargs)

    assert reader.file_path == str(resolve_config_path("tests/fixture/train.cnt"))
    assert reader._chunk_size == 100


@pytest.mark.parametrize(
    ("source", "default_port"),
    [(scan45.Scan45DataReader, 4000), (curry8.Curry8DataReader, 4455)],
)
def test_network_reader_explicit_parameters_and_defaults(source, default_port):
    reader = source(
        parameters={"DATA_PROVIDER_IP": "192.0.2.8", "DATA_PROVIDER_PORT": 9001},
        context=PluginContext(sample_rate=500),
    )
    default_reader = source(parameters={})
    context_only_reader = source(context=PluginContext())

    assert (reader.data_provider_ip, reader.data_provider_port) == ("192.0.2.8", 9001)
    assert not reader.is_connected
    for default in (default_reader, context_only_reader):
        assert (default.data_provider_ip, default.data_provider_port) == (
            "127.0.0.1", default_port,
        )


def test_curry_retains_positional_host_port_timeout():
    reader = curry8.Curry8DataReader("192.0.2.4", 5678, 0.25)

    assert reader.data_provider_ip == "192.0.2.4"
    assert reader.data_provider_port == 5678
    assert reader.timeout == 0.25


@pytest.mark.parametrize(
    ("parameters", "expected_address"),
    [({}, 0x3100), ({"parallel_port_address": "0x378"}, 0x378)],
)
def test_trigger_explicit_configuration_uses_windows_driver(
    monkeypatch, parameters, expected_address,
):
    support = import_module(".._inpout", scan45.__name__)
    port_class = Mock()
    monkeypatch.setattr(support, "PParallelInpOut", port_class)
    monkeypatch.setattr(ctypes, "windll", Mock(), raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(scan45.time, "sleep", Mock())

    trigger = scan45.Scan45Trigger(parameters=parameters, context=PluginContext())
    trigger.send(7)

    port_class.assert_called_once_with(expected_address)
    assert trigger.port.setData.call_args_list == [call(0), call(7)]


def test_trigger_retains_positional_address(monkeypatch):
    open_port = Mock()
    monkeypatch.setattr(scan45.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(scan45.Scan45Trigger, "clear", Mock())

    trigger = scan45.Scan45Trigger("0x3bc")

    assert trigger.address == 0x3BC
    open_port.assert_called_once_with(0x3BC)
