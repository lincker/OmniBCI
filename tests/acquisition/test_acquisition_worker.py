"""Unit tests for the AcquisitionWorker process boundary orchestration."""

from unittest.mock import MagicMock, patch

import pytest

from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker


def test_run_loads_source_before_running_runtime():
    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot={})
    calls = []
    worker._load_data_source = lambda: calls.append("load")
    worker._run = lambda: calls.append("run")

    with patch("omnibci.conf.settings.use_snapshot"):
        worker.run()

    assert calls == ["load", "run"]


@pytest.mark.parametrize("processing_input", [None, ProcessingInput().to_dict()])
def test_run_constructs_runtime_with_explicit_context(processing_input):
    command_queue = MagicMock()
    data_manager = MagicMock()
    event_bus = MagicMock()
    data_source = MagicMock(spec=SignalSource)
    snapshot = {
        "ACQUISITION.signal_source": "selected-source",
        "CONFIG.generation": "generation", "CONFIG.revision": "revision",
        "PLUGINS.bindings": [{"slot": "processing.decoder", "processing_input": processing_input}],
    }
    worker = AcquisitionWorker(command_queue, data_manager, event_bus, settings_snapshot=snapshot)
    worker.data_source = data_source
    snapshot["ACQUISITION.signal_source"] = "changed-source"

    with patch(
        "omnibci.infrastructure.acquisition.worker.AcquisitionRuntime"
    ) as runtime_class:
        worker._run()

    runtime_class.assert_called_once_with(
        data_source=data_source,
        command_queue=command_queue,
        data_manager=data_manager,
        event_bus=event_bus,
        source_id="selected-source",
        preparation_required=True,
        generation="generation",
        config_revision="revision",
    )
    runtime_class.return_value.run.assert_called_once_with()
    assert worker.runtime is runtime_class.return_value


@pytest.mark.parametrize("plugin_type", [None, "plugin-source"])
def test_runtime_source_identity_falls_back_to_plugin_type_or_class_name(plugin_type):
    class Source:
        pass

    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot={})
    worker.data_source = Source()
    if plugin_type is not None:
        worker.data_source.plugin_type = plugin_type
    with patch("omnibci.infrastructure.acquisition.worker.AcquisitionRuntime") as runtime_class:
        worker._run()

    assert runtime_class.call_args.kwargs["source_id"] == (plugin_type or "Source")
    assert runtime_class.call_args.kwargs["preparation_required"] is False


def test_global_settings_are_pinned_once_for_source_and_runtime():
    snapshot = {
        "ACQUISITION.signal_source": "global-source",
        "CONFIG.generation": "global-generation", "CONFIG.revision": "global-revision",
        "PLUGINS.bindings": [{"slot": "processing.decoder", "processing_input": ProcessingInput().to_dict()}],
    }
    worker = AcquisitionWorker(command_queue=MagicMock())
    with patch("omnibci.conf.settings.snapshot", side_effect=[snapshot, {}]) as read_snapshot, patch(
        "omnibci.conf.settings.use_snapshot"
    ) as install_snapshot, patch(
        "omnibci.infrastructure.plugins.runtime_configuration.load_configured_plugin"
    ) as load_source, patch(
        "omnibci.infrastructure.acquisition.worker.AcquisitionRuntime"
    ) as runtime_class:
        worker.run()

    read_snapshot.assert_called_once_with()
    install_snapshot.assert_called_once_with(snapshot)
    load_source.assert_called_once_with(snapshot, "acquisition.source", "acquisition", SignalSource)
    runtime_class.assert_called_once_with(
        data_source=load_source.return_value, command_queue=worker.command_queue,
        data_manager=None, event_bus=None, source_id="global-source", preparation_required=True,
        generation="global-generation", config_revision="global-revision",
    )


def test_historical_decoder_projection_requires_preparation():
    from omnibci.application.plugin_profiles import PluginSelection, ProfileBinding
    from omnibci.application.processing_source import materialize_processing_binding

    binding = ProfileBinding("processing.decoder", "decoder", None,
                             PluginSelection("test.decoder", "default", "1"), {})
    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot={
        "ACQUISITION.signal_source": "test-source",
        "PLUGINS.bindings": [materialize_processing_binding(binding)],
    })
    worker.data_source = MagicMock(spec=SignalSource)
    with patch("omnibci.infrastructure.acquisition.worker.AcquisitionRuntime") as runtime_class:
        worker._run()

    assert runtime_class.call_args.kwargs["preparation_required"] is True
    assert binding.processing_input is None


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
    with patch.object(
        PluginLoader, "load_entrypoint", wraps=PluginLoader.load_entrypoint
    ) as load_entrypoint:
        worker._load_data_source()

    load_entrypoint.assert_called_once_with(descriptor, SignalSource)
    assert worker.data_source._chunk_size == 37
    assert worker.data_source.file_path.endswith("selected.cnt")
