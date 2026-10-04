"""Configured workers acknowledge initialization before the engine is ready."""

import asyncio
import multiprocessing as mp
import threading
from multiprocessing.connection import wait
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnibci.infrastructure.acquisition.worker import AcquisitionWorker
from omnibci.infrastructure.acquisition.acquisition import AcquisitionCommand
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.ipc.event_bus import EventBus
from omnibci.infrastructure.neuroengine.engine import NeuroEngine
from omnibci.infrastructure.neuroengine.processing.worker import SignalProcessingWorker
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessorCommand
from omnibci.infrastructure.process_lifecycle import (
    ChildProcessStartupError, ChildProcessStartupTimeout, StartupChannel,
    StartupReport, StartupReporter, wait_for_startup_report,
)


@pytest.mark.parametrize("worker_type,service_path,run_name", [
    (AcquisitionWorker, "omnibci.infrastructure.acquisition.worker.Acquisition", "_run"),
    (SignalProcessingWorker, "omnibci.infrastructure.neuroengine.processing.worker.SignalProcessor", "_run_processor"),
])
def test_worker_reports_ready_after_construction_before_loop(worker_type, service_path, run_name):
    reporter = MagicMock(spec=StartupReporter)
    worker = worker_type(MagicMock(), startup_reporter=reporter,
                         settings_snapshot={"CONFIG.revision": "r", "CONFIG.generation": "g"})
    worker.data_source = MagicMock()
    with patch(service_path) as service:
        service.return_value.run.side_effect = lambda: reporter.ready.assert_called_once_with()
        reporter.ready.side_effect = lambda: service.assert_called_once()
        if run_name == "_run":
            worker._run()
        else:
            worker._run_processor(None, None, [], 250)
        assert service.call_args.kwargs["generation"] == "g"
        assert service.call_args.kwargs["config_revision"] == "r"


@pytest.mark.parametrize("worker_type,loader,stage", [
    (AcquisitionWorker, "_load_data_source", "signal_source"),
    (SignalProcessingWorker, "_load_algorithm", "plugins"),
])
@pytest.mark.parametrize("error", [ValueError("invalid plugin"), SystemExit(7)])
def test_worker_reports_all_startup_failures_and_closes(worker_type, loader, stage, error):
    reporter = MagicMock(spec=StartupReporter)
    worker = worker_type(MagicMock(), startup_reporter=reporter)
    setattr(worker, loader, MagicMock(side_effect=error))
    with pytest.raises(type(error)):
        worker.run()
    reporter.failed.assert_called_once_with(stage, error)
    reporter.ready.assert_not_called()
    reporter.close.assert_called_once_with()


@pytest.mark.parametrize("worker_type,loader,service_path,stage", [
    (AcquisitionWorker, "_load_data_source", "omnibci.infrastructure.acquisition.worker.Acquisition", "acquisition"),
    (SignalProcessingWorker, "_load_algorithm", "omnibci.infrastructure.neuroengine.processing.worker.SignalProcessor", "processor"),
])
def test_service_constructor_failure_never_reports_ready(worker_type, loader, service_path, stage):
    reporter = MagicMock(spec=StartupReporter)
    worker = worker_type(MagicMock(), startup_reporter=reporter)
    worker.data_source = MagicMock()
    setattr(worker, loader, MagicMock(return_value=(None, None, [])))
    error = ValueError("invalid service configuration")
    with patch(service_path, side_effect=error), pytest.raises(ValueError):
        worker.run()
    reporter.failed.assert_called_once_with(stage, error)
    reporter.ready.assert_not_called()
    reporter.close.assert_called_once_with()


def _engine_with_channels(*, delayed=None, stale=False, timeout=1):
    workers = [MagicMock(), MagicMock()]
    channels = []
    for index, (name, process) in enumerate(zip(("Acquisition", "Processing"), workers)):
        process.is_alive.return_value = True
        process.join.side_effect = lambda **kw: [setattr(p.is_alive, "return_value", False) for p in workers]
        receiver = MagicMock()
        receiver.poll.return_value = True
        if index == 1 and delayed is not None:
            receiver.poll.side_effect = delayed.wait
        receiver.recv.return_value = StartupReport.ready(
            name, config_revision="old" if stale else "r", generation="g",
        )
        channels.append(StartupChannel(name, process, receiver, MagicMock(spec=StartupReporter)))
    event_bus = MagicMock(start=AsyncMock(), stop=AsyncMock())
    gateway = MagicMock(start=AsyncMock(), stop=AsyncMock())
    engine = NeuroEngine(event_bus, MagicMock(), workers[0], MagicMock(), workers[1],
                         MagicMock(), gateway, startup_channels=tuple(channels),
                         startup_timeout_s=timeout, config_revision="r", generation="g")
    return engine, channels


@pytest.mark.asyncio
async def test_engine_waits_for_both_workers_without_blocking_event_loop():
    delayed = threading.Event()
    engine, channels = _engine_with_channels(delayed=delayed)
    task = asyncio.create_task(engine._start())
    try:
        await asyncio.sleep(0.03)
        assert not task.done()
        assert not engine._started
        engine.event_bus.start.assert_awaited_once()
        engine.peer_gateway.start.assert_awaited_once()
        for channel in channels:
            channel.reporter.close.assert_called_once()
        delayed.set()
        await task
        assert engine._started
        for channel in channels:
            channel.receiver.close.assert_called_once()
    finally:
        delayed.set()
        await task
        await engine._stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stale", "timeout"])
async def test_engine_failed_readiness_rolls_back_every_started_resource(failure):
    engine, channels = _engine_with_channels(
        stale=failure == "stale", delayed=threading.Event() if failure == "timeout" else None,
        timeout=0.01,
    )
    with pytest.raises(ChildProcessStartupError if failure == "stale" else ChildProcessStartupTimeout):
        await engine._start()
    assert not engine._started
    assert engine.shutdown_complete
    engine.peer_gateway.stop.assert_awaited_once()
    engine.event_bus.stop.assert_awaited_once()
    for channel in channels:
        assert channel.receiver.close.called
        assert channel.reporter.close.called


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_worker", ["acquisition_worker", "signal_processing_worker"])
async def test_process_start_failure_closes_all_child_channels(failed_worker):
    engine, channels = _engine_with_channels()
    getattr(engine, failed_worker).start.side_effect = RuntimeError("spawn failed")
    with pytest.raises(RuntimeError, match="spawn failed"):
        await engine._start()
    if failed_worker == "acquisition_worker":
        engine.signal_processing_worker.start.assert_not_called()
    for channel in channels:
        channel.receiver.close.assert_called_once()
        assert channel.reporter.close.called
    assert engine.shutdown_complete


@pytest.mark.asyncio
async def test_worker_must_still_be_alive_when_other_worker_becomes_ready():
    delayed = threading.Event()
    engine, _ = _engine_with_channels(delayed=delayed)
    task = asyncio.create_task(engine._start())
    await asyncio.sleep(0.03)
    engine.acquisition_worker.is_alive.return_value = False
    delayed.set()
    with pytest.raises(ChildProcessStartupError, match="before all workers"):
        await task
    assert engine.shutdown_complete


def _pinned_worker_snapshot(broken_kind=None):
    from dataclasses import replace
    from omnibci.application.plugin_profiles import ProfileBinding
    from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
    snapshot = {"CONFIG.revision": "r", "CONFIG.generation": "g", "ACQUISITION.sample_rate": 250}
    bindings = []
    for slot, kind, alias, parameters in (
        ("acquisition.source", "acquisition", "file_reader", {"sample_rate": 250, "channel_number": 10}),
        ("processing.decoder", "decoder", "THUBCI_SSVEP_classic40CCA", {}),
        ("processing.epoch_detector", "epoch_detector", "DEFAULT_EPOCH_DETECTOR", {}),
    ):
        descriptor = PluginCatalog().resolve(kind, alias)
        selection = descriptor.selection
        if kind == broken_kind:
            selection = replace(selection, package_id="missing-plugin")
        bindings.append(ProfileBinding(slot, kind, None, selection, parameters).to_dict())
    snapshot["PLUGINS.bindings"] = bindings
    return snapshot


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["acquisition", "processing"])
@pytest.mark.parametrize("broken", [False, True])
def test_spawned_configured_worker_reports_real_plugin_initialization(kind, broken):
    context = mp.get_context("spawn")
    commands, data, events = (context.Queue() for _ in range(3))
    receiver, sender = context.Pipe(duplex=False)
    component = "Acquisition" if kind == "acquisition" else "Processing"
    reporter = StartupReporter(sender, component=component, config_revision="r", generation="g")
    broken_kind = ("acquisition" if kind == "acquisition" else "decoder") if broken else None
    snapshot = _pinned_worker_snapshot(broken_kind)
    worker_type = AcquisitionWorker if kind == "acquisition" else SignalProcessingWorker
    worker = worker_type(commands, DataManager(buffer=data), EventBus(events),
                         settings_snapshot=snapshot, startup_reporter=reporter)
    # A spawn context pickles the complete worker bound method, including queues,
    # EventBus and its reporter, even on hosts whose default context is fork.
    process = context.Process(target=worker.run)
    try:
        process.start()
        reporter.close()
        if broken:
            with pytest.raises(ChildProcessStartupError, match="missing-plugin") as raised:
                wait_for_startup_report(receiver, process, component=component, timeout_s=15,
                                        expected_revision="r", expected_generation="g")
            assert raised.value.report.stage == ("signal_source" if kind == "acquisition" else "decoder")
        else:
            report = wait_for_startup_report(receiver, process, component=component, timeout_s=15,
                                              expected_revision="r", expected_generation="g")
            assert report.generation == "g"
            # Windows poll() may raise BrokenPipeError after the sender closes.
            assert wait([receiver], timeout=1) == [receiver]
            with pytest.raises(EOFError):
                receiver.recv()
            commands.put(AcquisitionCommand.stop_acquisition if kind == "acquisition"
                         else SignalProcessorCommand.should_stop)
        process.join(timeout=10)
        assert process.exitcode == (1 if broken else 0)
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
async def test_builder_manages_real_worker_startup_and_cleanup(broken):
    from omnibci.infrastructure.neuroengine.builder import NeuroEngineBuilder
    engine = NeuroEngineBuilder(settings_snapshot=_pinned_worker_snapshot(
        "decoder" if broken else None,
    )).build()
    engine.peer_gateway = MagicMock(start=AsyncMock(), stop=AsyncMock())
    try:
        if broken:
            with pytest.raises(ChildProcessStartupError, match="missing-plugin"):
                await engine._start()
            assert not engine._started
        else:
            await engine._start()
            assert engine._started
            assert engine.acquisition_worker.is_alive()
            assert engine.signal_processing_worker.is_alive()
        assert all(channel.receiver.closed for channel in engine._startup_channels)
    finally:
        await engine._stop()
    assert engine.shutdown_complete
