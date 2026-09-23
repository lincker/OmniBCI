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


def test_load_data_source_uses_pinned_parameters_without_registry_or_global_settings(monkeypatch):
    from omnibci.application.plugin_profiles import ProfileBinding
    from omnibci.conf import settings
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
    from omnibci.infrastructure.plugins.loader import PluginLoader

    descriptor = PluginCatalog().resolve_legacy("acquisition", "file_reader")
    binding = ProfileBinding(
        slot="acquisition.source", kind="acquisition", profile_id=None,
        selection=descriptor.selection,
        parameters={"data_file_path": "selected.cnt", "file_reader_chunk_size": 37,
                    "sample_rate": 250, "channel_number": 10},
    )
    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot={
        "PLUGINS.bindings": [binding.to_dict()], "ACQUISITION.sample_rate": 250,
        "ACQUISITION.signal_source": "must_not_be_used",
    })
    monkeypatch.setattr(settings, "get", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("global settings read")))
    monkeypatch.setattr(PluginLoader, "load_from_dir", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("recursive scan")))
    worker._load_data_source()

    assert worker.data_source._chunk_size == 37
    assert worker.data_source.file_path.endswith("selected.cnt")
