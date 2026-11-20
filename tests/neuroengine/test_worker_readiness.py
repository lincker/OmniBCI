"""Configured modules acknowledge initialization independently of the engine."""
from tests.plugin_configuration_helpers import builtin_selection

import asyncio
import multiprocessing as mp
import threading
from pathlib import Path
from multiprocessing.connection import wait
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError, ChildProcessStartupTimeout, StartupChannel,
    StartupReport, StartupReporter, wait_for_startup_report,
)


@pytest.mark.parametrize("worker_type,service_path,run_name", [
    (AcquisitionWorker, "omnibci.infrastructure.acquisition.worker.AcquisitionRuntime", "_run"),
    (SignalProcessingWorker, "omnibci.infrastructure.signal_processing.worker.SignalProcessor", "_run_processor"),
])
def test_worker_reports_ready_after_construction_before_loop(worker_type, service_path, run_name):
    reporter = MagicMock(spec=StartupReporter)
    worker = worker_type(MagicMock(), startup_reporter=reporter,
                         settings_snapshot={"CONFIG.module_revisions": {"acquisition": "r", "processing": "r", "stimulation": "r", "actuation": "r"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "g"})
    worker.data_source = MagicMock()
    with patch(service_path) as service:
        service.return_value.run.side_effect = lambda: reporter.ready.assert_called_once_with()
        reporter.ready.side_effect = lambda: service.assert_called_once()
        if run_name == "_run":
            worker._run()
        else:
            worker._run_processor()
        assert service.call_args.kwargs["generation"] == "g"
        assert service.call_args.kwargs["acquisition_revision"] == "r"


@pytest.mark.parametrize("worker_type,loader,stage", [
    (AcquisitionWorker, "_load_data_source", "signal_source"),
    (SignalProcessingWorker, "_run_processor", "processor"),
])
@pytest.mark.parametrize("error", [ValueError("invalid plugin"), SystemExit(7)])
def test_worker_reports_all_startup_failures_and_closes(worker_type, loader, stage, error):
    reporter = MagicMock(spec=StartupReporter)
    worker = worker_type(MagicMock(), startup_reporter=reporter, settings_snapshot={})
    setattr(worker, loader, MagicMock(side_effect=error))
    with pytest.raises(type(error)):
        worker.run()
    reporter.failed.assert_called_once_with(stage, error)
    reporter.ready.assert_not_called()
    reporter.close.assert_called_once_with()


@pytest.mark.parametrize("worker_type,loader,service_path,stage", [
    (AcquisitionWorker, "_load_data_source", "omnibci.infrastructure.acquisition.worker.AcquisitionRuntime", "acquisition"),
    (SignalProcessingWorker, None, "omnibci.infrastructure.signal_processing.worker.SignalProcessor", "processor"),
])
def test_service_constructor_failure_never_reports_ready(worker_type, loader, service_path, stage):
    reporter = MagicMock(spec=StartupReporter)
    worker = worker_type(MagicMock(), startup_reporter=reporter, settings_snapshot={})
    worker.data_source = MagicMock()
    if loader:
        setattr(worker, loader, MagicMock(return_value={}))
    error = ValueError("invalid service configuration")
    with patch(service_path, side_effect=error), pytest.raises(ValueError):
        worker.run()
    reporter.failed.assert_called_once_with(stage, error)
    reporter.ready.assert_not_called()
    reporter.close.assert_called_once_with()


def _engine_with_channels(*, delayed=None, stale=False, timeout=1):
    workers = [MagicMock(), MagicMock()]
    channels = []
    for index, (name, process) in enumerate(zip(("Acquisition", "Signal Processing"), workers)):
        process.is_alive.return_value = True
        process.join.side_effect = lambda process=process, **kw: setattr(process.is_alive, "return_value", False)
        receiver = MagicMock()
        receiver.poll.return_value = True
        if index == 1 and delayed is not None:
            receiver.poll.side_effect = delayed.wait
        receiver.recv.return_value = StartupReport.ready(
            name, module_revisions={"acquisition" if index == 0 else "processing": "old" if stale else "r"}, engine_revision="engine", generation="g",
        )
        channels.append(StartupChannel(name, process, receiver, MagicMock(spec=StartupReporter)))
    event_bus = MagicMock(start=AsyncMock(), stop=AsyncMock())
    gateway = MagicMock(start=AsyncMock(), stop=AsyncMock())
    engine = NeuroEngine(event_bus, MagicMock(), workers[0], MagicMock(), workers[1],
                         MagicMock(), gateway, startup_channels=tuple(channels),
                         startup_timeout_s=timeout, module_revisions={"acquisition": "r", "processing": "r"}, engine_revision="engine", generation="g")
    return engine, channels


_SNAPSHOT = {"CONFIG.module_revisions": {"acquisition": "r", "processing": "r"},
             "CONFIG.engine_revision": "engine", "CONFIG.generation": "g"}


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_index", [0, 1])
async def test_worker_exit_keeps_foundation_and_other_module_running(worker_index):
    engine, channels = _engine_with_channels()
    task = asyncio.create_task(engine.serve())
    try:
        assert await asyncio.to_thread(engine._startup_complete.wait, 1)
        for module in ("acquisition", "processing"):
            await engine.start_module(module, _SNAPSHOT)
        channel = channels[worker_index]
        channel.process.exitcode = 7
        channel.process.is_alive.return_value = False
        await asyncio.sleep(1.1)
        module = ("acquisition", "processing")[worker_index]
        other = ("processing", "acquisition")[worker_index]
        assert not engine.is_module_running(module)
        assert engine.is_module_running(other)
        assert not task.done()
        engine.event_bus.stop.assert_not_awaited()
        engine.peer_gateway.stop.assert_not_awaited()
    finally:
        engine.should_stop = True
        await task
    assert engine.shutdown_complete


@pytest.mark.asyncio
async def test_module_waits_for_own_readiness_without_blocking_foundation():
    delayed = threading.Event()
    engine, channels = _engine_with_channels(delayed=delayed)
    await engine._start()
    await engine.start_module("acquisition", _SNAPSHOT)
    task = asyncio.create_task(engine.start_module("processing", _SNAPSHOT))
    try:
        await asyncio.sleep(0.03)
        assert not task.done()
        assert engine._started
        assert engine.is_module_running("acquisition")
        assert not engine.is_module_running("processing")
        delayed.set()
        await task
        assert engine.is_module_running("processing")
        for channel in channels:
            channel.receiver.close.assert_called_once()
    finally:
        delayed.set()
        await task
        await engine._stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stale", "timeout"])
async def test_failed_module_readiness_leaves_foundation_running(failure):
    engine, channels = _engine_with_channels(
        stale=failure == "stale", delayed=threading.Event() if failure == "timeout" else None,
        timeout=0.01,
    )
    await engine._start()
    try:
        with pytest.raises(ChildProcessStartupError if failure == "stale" else ChildProcessStartupTimeout):
            await engine.start_module("processing", _SNAPSHOT)
        assert engine._started
        assert not engine.is_module_running("processing")
        engine.peer_gateway.stop.assert_not_awaited()
        engine.event_bus.stop.assert_not_awaited()
        channels[1].receiver.close.assert_called_once()
        channels[0].process.start.assert_not_called()
    finally:
        await engine._stop()
    assert engine.shutdown_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("module,index", [("acquisition", 0), ("processing", 1)])
async def test_process_start_failure_closes_own_child_channels(module, index):
    engine, channels = _engine_with_channels()
    channels[index].process.start.side_effect = RuntimeError("spawn failed")
    await engine._start()
    try:
        with pytest.raises(RuntimeError, match="spawn failed"):
            await engine.start_module(module, _SNAPSHOT)
        channels[index].receiver.close.assert_called_once()
        assert not engine.is_module_running(module)
        assert engine._started
    finally:
        await engine._stop()
    assert engine.shutdown_complete


@pytest.mark.asyncio
async def test_module_must_still_be_alive_when_its_readiness_completes():
    delayed = threading.Event()
    engine, _ = _engine_with_channels(delayed=delayed)
    await engine._start()
    task = asyncio.create_task(engine.start_module("processing", _SNAPSHOT))
    try:
        await asyncio.sleep(0.03)
        engine.signal_processing_worker.is_alive.return_value = False
        delayed.set()
        with pytest.raises(ChildProcessStartupError):
            await task
        assert engine._started
        assert not engine.is_module_running("processing")
    finally:
        delayed.set()
        await engine._stop()


@pytest.fixture
def prepared_input_decoder(tmp_path, monkeypatch):
    """A real decoder whose constructor requires prepared source information."""
    plugin_dir = tmp_path / "plugins"
    package = tmp_path / "source-readiness"
    marker = tmp_path / "constructed"
    package.mkdir(parents=True)
    (package / "manifest.toml").write_text(dedent('''
        manifest_version = 2
        [package]
        id = "readiness"
        publisher = "test"
        version = "1"
        api_version = "1"
domain = "processing"
        [[contributions]]
        id = "default"
        kind = "decoder"
        plugin_type = "readiness_decoder"
        entrypoint = "decoder:ReadinessDecoder"
        channel_requirements = {min_count = 1}
        [contributions.configuration]
        schema = "default.schema.json"
        schema_version = 1
    '''), encoding="utf-8")
    (package / "default.schema.json").write_text(
        '{"type":"object","properties":{},"additionalProperties":false}', encoding="utf-8",
    )
    (package / "decoder.py").write_text(dedent(f'''
        from pathlib import Path
        from omnibci.sdk import Decoder

        class ReadinessDecoder(Decoder):
            def setup(self):
                pass
            def begin_epoch(self):
                pass
            def teardown(self):
                pass
            def __init__(self, *, parameters, resource_root, input_signal):
                if input_signal is None:
                    raise ValueError("ReadinessDecoder requires prepared input")
                self.sample_rate = input_signal.sample_rate
                Path({str(marker)!r}).write_text("ready", encoding="utf-8")

            def should_decode(self, window):
                return False

            def decode(self, window, *, latest_raw_segment):
                return None
    '''), encoding="utf-8")
    monkeypatch.setenv("OMNIBCI_PLUGIN_DIR", str(plugin_dir))
    from tests.plugin_configuration_helpers import install_package_root
    install_package_root(package)
    return "readiness_decoder", marker


def _pinned_worker_snapshot(decoder_alias, broken_kind=None):
    from omnibci.application.configuration.models import ProfileBinding
    from omnibci.domain.channel_mapping import ProcessingInput
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
    from tests.plugin_configuration_helpers import prepared_snapshot
    snapshot = {"CONFIG.module_revisions": {"acquisition": "r", "processing": "r", "stimulation": "r", "actuation": "r"}, "CONFIG.engine_revision": "engine", "CONFIG.generation": "g", "ACQUISITION.sample_rate": 250,
                "ACQUISITION.signal_source": "file_reader"}
    bindings = []
    for slot, kind, alias, parameters in (
        ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 250, "channel_count": 10,
         "data_file_path": str(Path(__file__).parents[1] / "fixture" / "train.cnt")}),
        ("processing.decoder", "decoder", decoder_alias, {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
    ):
        descriptor = PluginCatalog().resolve_selection(kind, builtin_selection(kind, alias))
        selection = descriptor.selection
        bindings.append(ProfileBinding(slot, kind, None, selection, parameters,
            processing_input=ProcessingInput() if kind == "decoder" else None).to_dict())
    snapshot["PLUGINS.bindings"] = bindings
    snapshot = prepared_snapshot(snapshot)
    for binding in snapshot["PLUGINS.bindings"]:
        if binding["kind"] == broken_kind:
            binding["selection"]["package_id"] = "missing-plugin"
    return snapshot


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["acquisition", "processing"])
@pytest.mark.parametrize("broken", [False, True])
def test_spawned_configured_worker_reports_real_plugin_initialization(kind, broken, prepared_input_decoder):
    decoder_alias, constructed = prepared_input_decoder
    context = mp.get_context("spawn")
    commands, data, events = (context.Queue() for _ in range(3))
    receiver, sender = context.Pipe(duplex=False)
    component = "Acquisition" if kind == "acquisition" else "Signal Processing"
    reporter = StartupReporter(sender, component=component, module_revisions={kind: "r"}, engine_revision="engine", generation="g")
    broken_kind = ("acquisition" if kind == "acquisition" else "decoder") if broken else None
    snapshot = _pinned_worker_snapshot(decoder_alias, broken_kind)
    worker_type = AcquisitionWorker if kind == "acquisition" else SignalProcessingWorker
    worker = worker_type(commands, DataManager(buffer=data), EventBus(events),
                         settings_snapshot=snapshot, startup_reporter=reporter)
    # A spawn context pickles the complete worker bound method, including queues,
    # EventBus and its reporter, even on hosts whose default context is fork.
    process = context.Process(target=worker.run)
    try:
        process.start()
        reporter.close()
        if broken and kind == "acquisition":
            with pytest.raises(ChildProcessStartupError, match="missing-plugin") as raised:
                wait_for_startup_report(receiver, process, component=component, timeout_s=15,
                                        expected_module_revisions={kind: "r"}, expected_engine_revision="engine", expected_generation="g")
            assert raised.value.report.stage == ("signal_source" if kind == "acquisition" else "decoder")
        else:
            report = wait_for_startup_report(receiver, process, component=component, timeout_s=15,
                                              expected_module_revisions={kind: "r"}, expected_engine_revision="engine", expected_generation="g")
            assert report.generation == "g"
            if kind == "processing":
                assert not constructed.exists()
                from omnibci.application.events import CommandRequest
                from omnibci.domain.signal_info import SignalInfo, ChannelDescriptor
                from omnibci.domain.channel_mapping import ProcessingInput, compile_processing_plan
                from omnibci.shared.events import EventType
                source = SignalInfo(sample_rate=250, channels=(ChannelDescriptor(0, "O1", channel_id="channel-0"),))
                plan = compile_processing_plan(source, ProcessingInput(), acquisition_revision="r")
                receipt = {"generation": "g", "acquisition_revision": "r", "processing_revision": "r", "engine_revision": "engine", "connection_id": "connection",
                           "source_fingerprint": "f" * 64, "plan_fingerprint": plan.fingerprint, "input_id": "test-input"}
                commands.put((SignalProcessorCommand.prepare_signal, CommandRequest("prepare", {
                    "plan": plan.to_dict(), "receipt": receipt,
                })))
                result = events.get(timeout=15)
                assert result.type is EventType.CommandResult
                assert result.data.ok is not broken
                if broken:
                    assert "missing-plugin" in result.data.message
                    assert not constructed.exists()
                else:
                    assert constructed.read_text(encoding="utf-8") == "ready"
            # Windows poll() may raise BrokenPipeError after the sender closes.
            assert wait([receiver], timeout=1) == [receiver]
            with pytest.raises(EOFError):
                receiver.recv()
            commands.put((AcquisitionCommand.stop_acquisition if kind == "acquisition"
                         else SignalProcessorCommand.should_stop, None))
        process.join(timeout=10)
        assert process.exitcode == (1 if broken and kind == "acquisition" else 0)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
        reporter.close()
        receiver.close()
        for queue in (commands, data, events):
            queue.close()
            queue.join_thread()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [False, True])
async def test_builder_manages_real_worker_startup_and_cleanup(broken, prepared_input_decoder):
    from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
    decoder_alias, constructed = prepared_input_decoder
    snapshot = _pinned_worker_snapshot(decoder_alias, "decoder" if broken else None)
    engine = NeuroEngineBuilder(settings_snapshot=snapshot).build()
    engine.peer_gateway = MagicMock(start=AsyncMock(), stop=AsyncMock())
    try:
        await engine._start()
        assert engine._started
        assert not constructed.exists()
        assert engine.acquisition_worker is None
        assert engine.signal_processing_worker is None
        await engine.start_module("acquisition", snapshot)
        await engine.start_module("processing", snapshot)
        assert engine.acquisition_worker.is_alive()
        assert engine.signal_processing_worker.is_alive()
        assert all(channel.receiver.closed for channel in engine._startup_channels)
    finally:
        await engine._stop()
    assert engine.shutdown_complete
