"""Acquisition diagnostics cross spawn without sharing the BCI queues."""

import logging
import multiprocessing as mp
from queue import Queue
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import numpy as np
import pytest

from omnibci.domain.signal import SignalChunk
from omnibci.infrastructure.acquisition.logging import AcquisitionLogForwarder, AcquisitionLogListener
from omnibci.infrastructure.acquisition.preprocessing import TriggerEdgePreprocessor
from omnibci.infrastructure.backend_logging import BackendLogging


PREFIX = "omnibci.infrastructure.acquisition"


def emit_from_child(queue, stop):
    with AcquisitionLogForwarder(queue, source_id="scan45", generation="run-1"):
        logging.getLogger(PREFIX + ".acquisition").info("Trigger preprocessing reset: reason=stream_start")
        processor = TriggerEdgePreprocessor()
        processor.process(SignalChunk(123, 42, np.array([[1, 2, 3, 4], [7, 7, 8, 8]])))
        logging.getLogger("omnibci.unrelated").info("unrelated child record")
        # Real acquisition stays alive after delivering a chunk. Exit is a
        # best-effort boundary and may discard a pending queue feeder backlog.
        stop.wait(10)


@pytest.mark.integration
def test_spawned_trigger_summaries_reach_the_existing_backend_file(tmp_path):
    context = mp.get_context("spawn")
    queue = context.Queue(maxsize=16)
    stop = context.Event()
    listener = AcquisitionLogListener(queue)
    process = context.Process(target=emit_from_child, args=(queue, stop))
    try:
        with BackendLogging({"LOGGING.file_path": str(tmp_path / "events.log")}) as backend:
            listener.start()
            process.start()
            deadline = monotonic() + 5
            while "sequence_id=42" not in "\n".join(backend.read_tail()[0]):
                assert monotonic() < deadline, "The running worker's trigger summary was not logged"
                sleep(0.01)
            from fastapi.testclient import TestClient
            from omnibci.presentation.main import create_app

            app = create_app(api_token="")
            app.state.backend_logging = backend
            response = TestClient(app).get("/api/v1/system/logs")
            assert response.status_code == 200
            assert "sequence_id=42" in "\n".join(response.json()["data"]["lines"])
            stop.set()
            process.join(timeout=10)
            assert process.exitcode == 0
            assert listener.stop()
        text = backend.file_path.read_text()
        assert "sequence_id=42" in text and "suppressed=2" in text
        assert "reason=stream_start" in text
        assert "source_id=scan45" in text and "generation=run-1" in text
        assert f"worker_pid={process.pid}" in text
        assert "unrelated child record" not in text
    finally:
        stop.set()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        listener.stop()
        process.close()
        queue.cancel_join_thread()
        queue.close()


def test_full_channel_drops_diagnostics_without_raising_or_stderr(capsys):
    queue = Queue(maxsize=1)
    with AcquisitionLogForwarder(queue, source_id="source", generation="run") as forwarding:
        logger = logging.getLogger(PREFIX + ".preprocessing")
        for index in range(100):
            logger.info("summary=%s", index)
    assert queue.qsize() == 1
    assert forwarding.dropped_records == 99
    assert capsys.readouterr().err == ""


def test_broken_channel_does_not_raise_into_acquisition(capsys):
    def fail(_):
        raise OSError("closed channel")

    with AcquisitionLogForwarder(SimpleNamespace(put_nowait=fail), source_id="source", generation="run") as forwarding:
        logging.getLogger(PREFIX).info("diagnostic")
    assert forwarding.dropped_records == 1
    assert capsys.readouterr().err == ""


def flood_from_child(queue, report):
    with AcquisitionLogForwarder(queue, source_id="source", generation="run") as forwarding:
        for index in range(1000):
            logging.getLogger(PREFIX).info("summary=%s %s", index, "x" * 4096)
        report.send(forwarding.dropped_records)
    report.close()


@pytest.mark.integration
def test_a_full_process_queue_never_holds_up_worker_exit():
    context = mp.get_context("spawn")
    queue = context.Queue(maxsize=1)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=flood_from_child, args=(queue, send))
    try:
        process.start()
        send.close()
        assert receive.poll(5), "Worker was blocked by its diagnostic queue"
        assert receive.recv() == 999
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()
        receive.close()
        send.close()
        queue.cancel_join_thread()
        queue.close()


def test_forwarder_restores_existing_logging_configuration_after_failure():
    logger = logging.getLogger(PREFIX)
    previous = logger.level, logger.propagate, list(logger.handlers)
    try:
        with pytest.raises(RuntimeError, match="failure"):
            with AcquisitionLogForwarder(Queue(), source_id="source", generation="run"):
                assert logger.level == logging.INFO and not logger.propagate
                raise RuntimeError("failure")
        assert (logger.level, logger.propagate, logger.handlers) == previous
    finally:
        logger.setLevel(previous[0])
        logger.propagate = previous[1]


def test_forwarder_bounds_message_size_and_listener_respects_backend_level(tmp_path):
    queue = Queue()
    listener = AcquisitionLogListener(queue)
    with AcquisitionLogForwarder(queue, source_id="source", generation="run"):
        logging.getLogger(PREFIX).info("%s", "x" * 100_000)
    item = queue.get_nowait()
    assert len(item["msg"]) < 5000
    queue.put_nowait(item)
    with BackendLogging({"LOGGING.file_path": str(tmp_path / "events.log"), "LOGGING.level": "WARNING"}) as backend:
        listener.start()
        assert listener.stop()
    assert backend.file_path.read_text() == ""


def test_listener_ignores_other_logger_names_and_stops_after_channel_failure(tmp_path):
    queue = Queue()
    queue.put_nowait({"name": "other.library", "levelno": logging.INFO, "msg": "must not forward"})
    listener = AcquisitionLogListener(queue)
    with BackendLogging({"LOGGING.file_path": str(tmp_path / "events.log")}) as backend:
        listener.start()
        listener.start()
        assert listener.stop() and listener.stop()
    assert backend.file_path.read_text() == ""

    def fail(**kwargs):
        raise OSError("broken log queue")

    broken = AcquisitionLogListener(SimpleNamespace(get=fail))
    broken.start()
    assert broken.stop()
    assert broken.read_errors == 1


def test_listener_thread_start_failure_can_be_cleaned_up(monkeypatch):
    from omnibci.infrastructure.acquisition import logging as module

    def fail_start(self):
        raise RuntimeError("thread creation failed")

    monkeypatch.setattr(module.Thread, "start", fail_start)
    listener = AcquisitionLogListener(Queue())
    with pytest.raises(RuntimeError, match="thread creation failed"):
        listener.start()
    assert listener.stopped
    assert listener.stop()


def test_blocked_backend_handler_does_not_make_listener_stop_wait_forever(monkeypatch, caplog):
    queue = Queue()
    entered, release = Event(), Event()
    logger = logging.getLogger(PREFIX)
    with AcquisitionLogForwarder(queue, source_id="source", generation="run"):
        logger.info("diagnostic")

    def block(record):
        entered.set()
        release.wait(5)

    monkeypatch.setattr(logger, "handle", block)
    listener = AcquisitionLogListener(queue)
    try:
        with caplog.at_level(logging.INFO, logger=PREFIX):
            listener.start()
            assert entered.wait(2)
            started = monotonic()
            assert not listener.stop(timeout_s=0.02)
            assert monotonic() - started < 0.5
            assert not listener.stopped
            release.set()
            assert listener.stop()
    finally:
        release.set()
        listener.stop()


@pytest.mark.asyncio
async def test_engine_starts_log_consumer_before_workers_and_stops_it_after_them():
    from tests.neuroengine.test_neuro_engine import _make_engine

    calls = []
    listener = SimpleNamespace(stopped=True)

    def start():
        calls.append("log-start")
        listener.stopped = False

    def stop():
        calls.append("log-stop")
        listener.stopped = True
        return True

    listener.start, listener.stop = start, stop
    engine = _make_engine(acquisition_logs=listener)
    engine.acquisition_worker.start.side_effect = lambda: calls.append("worker-start")
    engine.acquisition_worker.join.side_effect = lambda **kwargs: calls.append("worker-join")
    await engine._start()
    engine.acquisition_worker.is_alive.return_value = False
    engine.signal_processing_worker.is_alive.return_value = False
    await engine._stop()
    assert calls.index("log-start") < calls.index("worker-start")
    assert calls.index("worker-join") < calls.index("log-stop")
    assert engine.shutdown_complete


@pytest.mark.asyncio
async def test_engine_startup_failure_still_stops_the_log_consumer():
    from tests.neuroengine.test_neuro_engine import _make_engine

    listener = AcquisitionLogListener(Queue())
    engine = _make_engine(acquisition_logs=listener)
    engine.acquisition_worker.start.side_effect = RuntimeError("start failed")
    with pytest.raises(RuntimeError, match="start failed"):
        await engine._start()
    assert listener.stopped and engine.shutdown_complete
