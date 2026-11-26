"""Unit tests for the AcquisitionWorker process boundary orchestration."""
import logging
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, patch

import pytest

from omnibci.application.events import CommandRequest
from omnibci.domain.channel_mapping import ProcessingInput
from omnibci.domain.ports.acquisition import SignalSource
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.shared.events import EventType
from tests.plugin_configuration_helpers import builtin_selection, prepared_snapshot


def _source_binding(package_id="test.source", contribution_id="default", version="1.0.0"):
    return {
        "slot": "acquisition.source", "kind": "acquisition",
        "selection": {"package_id": package_id, "contribution_id": contribution_id, "version": version},
        "parameters": {},
    }


def test_managed_worker_forwards_runtime_logs_with_source_context():
    records = Queue()
    worker = AcquisitionWorker(command_queue=Queue(), log_queue=records, settings_snapshot={
        "PLUGINS.bindings": [_source_binding()], "CONFIG.generation": "generation",
    })
    worker.data_source = MagicMock(spec=SignalSource, trigger_mode="events", resume_mode="restart-only")
    with patch("omnibci.infrastructure.acquisition.worker.AcquisitionRuntime") as runtime_class:
        runtime = runtime_class.return_value
        runtime.source_id, runtime.generation = "test.source/default@1.0.0", "generation"
        runtime.run.side_effect = lambda: logging.getLogger(
            "omnibci.infrastructure.acquisition.preprocessing"
        ).info("Trigger preprocessing: sequence_id=1 suppressed=2")
        worker._run()
    message = records.get_nowait()["msg"]
    assert "source_id=test.source/default@1.0.0 generation=generation" in message
    assert "sequence_id=1 suppressed=2" in message
    assert records.empty()


def test_run_loads_source_before_running_runtime():
    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot={})
    calls = []
    worker._load_data_source = lambda: calls.append("load")
    worker._run = lambda: calls.append("run")

    worker.run()

    assert calls == ["load", "run"]


@pytest.mark.parametrize("bindings", [
    [],
    [{"slot": "processing.decoder", "processing_input": {"schema_version": 2, "outputs": "invalid"}}],
])
def test_connect_does_not_require_decoder_configuration(bindings):
    commands = Queue()
    commands.put((AcquisitionCommand.connect, CommandRequest("connect")))
    commands.put((AcquisitionCommand.stop_acquisition, None))
    events = MagicMock()
    source = MagicMock(spec=SignalSource, trigger_mode="events", resume_mode="restart-only")
    source.trigger_mode = "events"
    source.resume_mode = "restart-only"
    worker = AcquisitionWorker(commands, MagicMock(), events, settings_snapshot={
        "PLUGINS.bindings": [_source_binding(), *bindings],
    })
    worker.data_source = source
    worker._run()

    source.connect.assert_awaited_once_with()
    result = next(call.args[0].data for call in events.put_event.call_args_list
                  if call.args[0].type is EventType.CommandResult)
    assert result.ok
    source.observe.assert_not_called()


@pytest.mark.parametrize("decoder", [
    None,
    {"slot": "processing.decoder", "kind": "decoder", "parameters": {},
     "selection": {"package_id": "missing.decoder", "contribution_id": "default"},
     "configuration_version": 1, "profile_revision": 0,
     "processing_input": {"schema_version": 2, "outputs": "invalid"}},
])
def test_file_source_worker_connects_with_unconfigured_processing(decoder):
    from omnibci.application.configuration.models import ProfileBinding
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog

    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "file_reader"))
    source = ProfileBinding("acquisition.source", "acquisition", None, descriptor.selection, {
        "data_file_path": str(Path(__file__).parents[1] / "fixture" / "train.cnt"),
    }).to_dict()
    commands = Queue()
    commands.put((AcquisitionCommand.connect, CommandRequest("connect")))
    commands.put((AcquisitionCommand.get_metadata, CommandRequest("metadata")))
    commands.put((AcquisitionCommand.stop_acquisition, None))
    events = MagicMock()
    worker = AcquisitionWorker(commands, MagicMock(), events, settings_snapshot=prepared_snapshot({
        "PLUGINS.bindings": [source] + ([decoder] if decoder else []),
    }, ("acquisition",)))
    worker.run()

    results = {call.args[0].data.command_id: call.args[0].data
               for call in events.put_event.call_args_list if call.args[0].type is EventType.CommandResult}
    assert results["connect"].ok, results["connect"].message
    assert results["metadata"].ok, results["metadata"].message
    assert results["metadata"].data["metadata"]
    assert results["metadata"].data["descriptor"]["sample_rate"] > 0
    assert worker.data_source.reader_adapter is None


@pytest.mark.parametrize("processing_input", [None, ProcessingInput().to_dict()])
def test_run_constructs_runtime_with_explicit_context(processing_input):
    command_queue = MagicMock()
    data_manager = MagicMock()
    event_bus = MagicMock()
    data_source = MagicMock(spec=SignalSource, trigger_mode="events", resume_mode="restart-only")
    snapshot = {
        "CONFIG.generation": "generation", "CONFIG.module_revisions": {"acquisition": "revision", "processing": "revision", "stimulation": "revision", "actuation": "revision"}, "CONFIG.engine_revision": "engine",
        "PLUGINS.bindings": [_source_binding(), {"slot": "processing.decoder", "processing_input": processing_input}],
    }
    worker = AcquisitionWorker(command_queue, data_manager, event_bus, settings_snapshot=snapshot)
    worker.data_source = data_source
    snapshot["PLUGINS.bindings"][0]["selection"]["version"] = "2.0.0"

    with patch(
        "omnibci.infrastructure.acquisition.worker.AcquisitionRuntime"
    ) as runtime_class:
        worker._run()

    runtime_class.assert_called_once_with(
        data_source=data_source,
        command_queue=command_queue,
        data_manager=data_manager,
        event_bus=event_bus,
        source_id="test.source/default@1.0.0",
        generation="generation",
        acquisition_revision="revision", engine_revision="engine",
        processing_active=None, runtime_counters=None, stop_event=None,
    )
    runtime_class.return_value.run.assert_called_once_with()
    assert worker.runtime is runtime_class.return_value


def test_runtime_source_identity_falls_back_to_class_name():
    class Source:
        pass

    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot={})
    worker.data_source = Source()
    with patch("omnibci.infrastructure.acquisition.worker.AcquisitionRuntime") as runtime_class:
        worker._run()

    assert runtime_class.call_args.kwargs["source_id"] == "Source"


def test_explicit_settings_are_shared_by_source_and_runtime():
    snapshot = {
        "CONFIG.generation": "generation", "CONFIG.module_revisions": {"acquisition": "revision"}, "CONFIG.engine_revision": "engine",
        "PLUGINS.bindings": [_source_binding(), {"slot": "processing.decoder", "processing_input": ProcessingInput().to_dict()}],
    }
    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot=snapshot)
    with patch(
        "omnibci.infrastructure.plugin_management.runtime.load_configured_plugin"
    ) as load_source, patch(
        "omnibci.infrastructure.acquisition.worker.AcquisitionRuntime"
    ) as runtime_class:
        worker.run()

    load_source.assert_called_once_with(snapshot, "acquisition.source", "acquisition", SignalSource)
    runtime_class.assert_called_once_with(
        data_source=load_source.return_value, command_queue=worker.command_queue,
        data_manager=None, event_bus=None, source_id="test.source/default@1.0.0",
        generation="generation", acquisition_revision="revision", engine_revision="engine",
        processing_active=None, runtime_counters=None, stop_event=None,
    )


def test_load_data_source_uses_pinned_parameters_without_registry_or_global_settings():
    from omnibci.application.configuration.models import ProfileBinding
    from omnibci.infrastructure.plugin_management.catalog import PluginCatalog
    from omnibci.infrastructure.plugin_management.loader import PluginLoader

    descriptor = PluginCatalog().resolve_selection("acquisition", builtin_selection("acquisition", "file_reader"))
    binding = ProfileBinding(
        slot="acquisition.source", kind="acquisition", profile_id=None,
        selection=descriptor.selection,
        parameters={"data_file_path": "selected.cnt", "file_reader_chunk_size": 37,
                    "sample_rate": 250, "channel_count": 10},
    )
    worker = AcquisitionWorker(command_queue=MagicMock(), settings_snapshot=prepared_snapshot({
        "PLUGINS.bindings": [binding.to_dict()], "ACQUISITION.sample_rate": 250,
    }))
    with patch.object(
        PluginLoader, "load_entrypoint", wraps=PluginLoader.load_entrypoint
    ) as load_entrypoint:
        worker._load_data_source()

    load_entrypoint.assert_called_once_with(descriptor, SignalSource)
    assert worker.data_source._chunk_size == 37
    assert worker.data_source.file_path.endswith("selected.cnt")
