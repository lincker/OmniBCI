"""Unit tests for the AcquisitionWorker process boundary orchestration."""

from unittest.mock import MagicMock, patch

from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker


def test_run_loads_source_before_running_service():
    worker = AcquisitionWorker(command_queue=MagicMock())
    calls = []
    worker._load_data_source = lambda: calls.append("load")
    worker._run = lambda: calls.append("run")

    worker.run()

    assert calls == ["load", "run"]


def test_run_constructs_service_with_shared_dependencies():
    command_queue = MagicMock()
    data_manager = MagicMock()
    event_bus = MagicMock()
    data_source = MagicMock(spec=SignalSource)
    worker = AcquisitionWorker(command_queue, data_manager, event_bus)
    worker.data_source = data_source

    with patch(
        "omnibci.infrastructure.acquisition.worker.Acquisition"
    ) as acquisition_class:
        worker._run()

    acquisition_class.assert_called_once_with(
        data_source=data_source,
        command_queue=command_queue,
        data_manager=data_manager,
        event_bus=event_bus,
    )
    acquisition_class.return_value.run.assert_called_once_with()
    assert worker.acquisitor is acquisition_class.return_value


def test_load_data_source_uses_configured_plugin_type():
    worker = AcquisitionWorker(command_queue=MagicMock())
    data_source = MagicMock(spec=SignalSource)

    with patch(
        "omnibci.infrastructure.plugins.loader.PluginLoader.load_from_dir"
    ) as load_from_dir, patch(
        "omnibci.conf.settings.get", return_value="file_reader"
    ) as settings_get, patch(
        "omnibci.infrastructure.plugins.registry.ObjectFactory.create",
        return_value=data_source,
    ) as create:
        worker._load_data_source()

    load_from_dir.assert_called_once()
    assert load_from_dir.call_args.kwargs == {
        "base_package": "omnibci.infrastructure.plugins.acquisition"
    }
    assert load_from_dir.call_args.args[0].endswith(
        "omnibci/infrastructure/plugins/acquisition"
    )
    settings_get.assert_called_once_with("ACQUISITION.signal_source")
    create.assert_called_once_with(SignalSource, "file_reader")
    assert worker.data_source is data_source
