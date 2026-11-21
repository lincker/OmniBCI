"""Explicit acquisition constructors do not consult process-global settings."""

from pathlib import Path
import ctypes
import sys
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from omnibci.infrastructure.plugins.acquisition.file import reader as file_module
from omnibci.infrastructure.plugins.acquisition.neuroscan.curry8 import curry8
from omnibci.infrastructure.plugins.acquisition.neuroscan.scan45 import scan45


@pytest.fixture(autouse=True)
def forbid_settings_reads(monkeypatch):
    getter = Mock(side_effect=AssertionError("Explicit plugins must not read settings"))
    for module in (file_module, curry8, scan45):
        monkeypatch.setattr(module, "settings", SimpleNamespace(get=getter), raising=False)
    return getter


def test_file_reader_uses_explicit_path_and_chunk_size(tmp_path):
    path = tmp_path / "subject.cnt"
    parameters = {"data_file_path": str(path), "file_reader_chunk_size": 37}

    reader = file_module.FileReader(parameters=parameters, resource_root=Path(__file__).parent)
    parameters["file_reader_chunk_size"] = 99

    assert reader.file_path == str(path)
    assert reader._chunk_size == 37
    assert reader.file_pointer == 0
    assert reader.reader_adapter is None


def test_file_reader_explicit_defaults_never_fall_back_to_settings():
    with pytest.raises(ValueError, match="Select a recording file"):
        file_module.FileReader(parameters={}, resource_root=Path(__file__).parent)


@pytest.mark.parametrize(
    ("source", "default_port"),
    [(scan45.Scan45DataReader, 4000), (curry8.Curry8DataReader, 4455)],
)
def test_network_reader_explicit_parameters_and_defaults(source, default_port):
    reader = source(
        parameters={"DATA_PROVIDER_IP": "192.0.2.8", "DATA_PROVIDER_PORT": 9001, "sample_rate": 500},
        resource_root=Path(__file__).parent,
    )
    default_reader = source(parameters={}, resource_root=Path(__file__).parent)

    assert (reader.data_provider_ip, reader.data_provider_port) == ("192.0.2.8", 9001)
    assert reader.reader is None and reader.writer is None
    assert (default_reader.data_provider_ip, default_reader.data_provider_port) == (
        "127.0.0.1", default_port,
    )


def test_curry_host_port_timeout_are_explicit_parameters():
    reader = curry8.Curry8DataReader(parameters={'DATA_PROVIDER_IP': '192.0.2.4', 'DATA_PROVIDER_PORT': 5678, 'timeout': 0.25}, resource_root=Path(__file__).parent)

    assert reader.data_provider_ip == "192.0.2.4"
    assert reader.data_provider_port == 5678
    assert reader.timeout == 0.25


@pytest.mark.parametrize("timeout", [0, -1, True, None, "5", float("nan"), float("inf")])
def test_curry_rejects_invalid_network_timeout_before_connecting(timeout):
    with pytest.raises(ValueError, match="timeout must be positive"):
        curry8.Curry8DataReader(parameters={"timeout": timeout}, resource_root=Path(__file__).parent)


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

    trigger = scan45.Scan45Trigger(parameters=parameters, resource_root=Path(__file__).parent)
    trigger.setup()
    trigger.clear()
    trigger.send(7)

    port_class.assert_called_once_with(expected_address)
    assert trigger.port.setData.call_args_list == [call(0), call(7)]


def test_trigger_address_comes_only_from_parameters(monkeypatch):
    open_port = Mock()
    monkeypatch.setattr(scan45.Scan45Trigger, "_set_parallel_port", open_port)
    monkeypatch.setattr(scan45.Scan45Trigger, "clear", Mock())

    trigger = scan45.Scan45Trigger(parameters={'parallel_port_address': '0x3bc'}, resource_root=Path(__file__).parent)

    assert trigger.address == 0x3BC
    open_port.assert_not_called()
    monkeypatch.setattr(sys, "platform", "win32")
    trigger.setup()
    open_port.assert_called_once_with(0x3BC)


@pytest.mark.parametrize("source", [file_module.FileReader, scan45.Scan45DataReader,
                                   curry8.Curry8DataReader, scan45.Scan45Trigger])
def test_implicit_configuration_is_rejected(source):
    with pytest.raises(TypeError, match="parameters"):
        source()
