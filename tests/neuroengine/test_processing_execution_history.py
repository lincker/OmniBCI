"""Only constructed Processing pipelines produce private execution evidence."""
from copy import deepcopy
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.events import CommandRequest, SignalPreparationReceipt
from omnibci.application.plugin_identity import PluginSelection
from omnibci.domain.channel_mapping import ProcessingInput, compile_processing_plan
from omnibci.infrastructure.plugins.execution_history import PluginExecutionHistory
from omnibci.infrastructure.plugins.preparation import execution_receipt
from omnibci.infrastructure.signal_processing import configuration_transactions, signal_configuration
from omnibci.infrastructure.signal_processing.pipeline_configuration import ConfiguredPipeline
from omnibci.infrastructure.signal_processing.signal_processor import SignalProcessor
from omnibci.infrastructure.signal_processing.worker import SignalProcessingWorker
from tests.execution_helpers import synthetic_execution_snapshot
from tests.processing_helpers import prepared_signal


@pytest.fixture
def prepared(monkeypatch):
    inputs = ProcessingInput()
    snapshot = synthetic_execution_snapshot({
        "CONFIG.generation": "run", "CONFIG.engine_revision": "b" * 64,
        "CONFIG.module_revisions": {"acquisition": "a" * 64, "processing": "c" * 64},
        "PLUGINS.bindings": [ProfileBinding(
            "processing.decoder", "decoder", None,
            PluginSelection("test.decoder", "default", "1.0.0"), {}, processing_input=inputs,
        ).to_dict()],
    })
    plan = compile_processing_plan(prepared_signal(250), inputs, acquisition_revision="a" * 64)
    receipt = SignalPreparationReceipt(
        generation="run", engine_revision="b" * 64,
        acquisition_revision="a" * 64, processing_revision="c" * 64,
        connection_id="connection", source_fingerprint="d" * 64,
        plan_fingerprint=plan.fingerprint, input_id="input-1",
    )
    events, records = [], []
    processor = SignalProcessor(None, None, Queue(), None, SimpleNamespace(put_event=events.append),
                                settings_snapshot=snapshot, record_execution=records.append)
    decoder = MagicMock()
    load = MagicMock(return_value=(decoder, MagicMock(), []))
    monkeypatch.setattr(signal_configuration, "load_signal_pipeline", load)
    request = CommandRequest("prepare", {"plan": plan.to_dict(), "receipt": receipt.to_dict()})
    return SimpleNamespace(processor=processor, snapshot=snapshot, request=request, decoder=decoder,
                           events=events, records=records, load=load)


def test_preparation_records_exact_evidence_once_after_setup(prepared):
    p = prepared
    def record(receipt):
        p.decoder.setup.assert_called_once_with()
        assert p.processor._decoder is None
        p.records.append(receipt)
    p.processor._record_execution = record
    assert not p.records
    p.processor._prepare_signal(p.request)
    p.processor._prepare_signal(p.request)
    assert all(event.data.ok for event in p.events)
    assert p.records == [execution_receipt(p.snapshot, "processing", "input-1")]
    p.load.assert_called_once()
    p.processor._close_decoders()


def test_failed_preparation_history_preserves_old_decoder(prepared):
    p = prepared
    old = MagicMock()
    p.processor._setup_decoder(old)
    p.processor._decoder = old
    p.processor._record_execution = MagicMock(side_effect=OSError("history unavailable"))
    p.processor._prepare_signal(p.request)
    assert not p.events[-1].data.ok
    assert "history unavailable" in p.events[-1].data.message
    assert p.processor._decoder is old
    old.teardown.assert_not_called()
    p.decoder.teardown.assert_called_once_with()
    assert p.processor._signal_receipt is None
    p.processor._close_decoders()


@pytest.mark.parametrize("failed_setup", [True, False])
def test_no_history_for_unprepared_pipeline(prepared, failed_setup):
    p = prepared
    if failed_setup:
        p.decoder.setup.side_effect = RuntimeError("setup failed")
    else:
        p.load.side_effect = RuntimeError("construction failed")
    p.processor._prepare_signal(p.request)
    assert not p.events[-1].data.ok
    assert not p.records
    p.processor._close_decoders()


@pytest.mark.parametrize("standby,write_fails", [(False, False), (False, True), (True, False)])
def test_staged_candidate_record_precedes_boundary_and_failed_write_keeps_old(
        prepared, monkeypatch, standby, write_fails):
    p = prepared
    if not standby:
        p.processor._prepare_signal(p.request)
        assert p.events[-1].data.ok
    p.records.clear()
    p.events.clear()
    old = p.processor._decoder
    candidate = None if standby else MagicMock()
    snapshot = deepcopy(p.snapshot)
    snapshot["CONFIG.module_revisions"]["processing"] = "d" * 64
    monkeypatch.setattr(configuration_transactions, "build_pipeline_update",
                        lambda *args, **kwargs: ConfiguredPipeline(candidate, (), snapshot))
    if not standby:
        p.processor.current_epoch = object()
    if write_fails:
        p.processor._record_execution = MagicMock(side_effect=OSError("history unavailable"))
    request = CommandRequest("update-1", {
        "settings_snapshot": snapshot, "expected_revision": "c" * 64,
        "generation": "run", "engine_revision": "b" * 64, "boundary": "trial",
    })
    p.processor._stage_configuration(request)
    if standby:
        assert p.events[-1].data.ok
        assert not p.records
    elif write_fails:
        assert not p.events[-1].data.ok
        assert "history unavailable" in p.events[-1].data.message
        assert p.processor._configuration_transactions.pending is None
        candidate.teardown.assert_called_once_with()
    else:
        assert p.records == [execution_receipt(snapshot, "processing", "update-1")]
        candidate.setup.assert_called_once_with()
        assert not p.events
        p.processor._stage_configuration(request)
        assert len(p.records) == 1
        p.processor._cancel_pending_configuration("cancelled before activation")
        assert len(p.records) == 1
    assert p.processor._decoder is old
    if old is not None:
        old.teardown.assert_not_called()
    p.processor._close_decoders()


def test_worker_constructs_writer_in_child_with_explicit_root(tmp_path, monkeypatch):
    import omnibci.infrastructure.signal_processing.worker as module
    processor_type = MagicMock()
    monkeypatch.setattr(module, "SignalProcessor", processor_type)
    root = tmp_path / "accepted"
    worker = SignalProcessingWorker(Queue(), settings_snapshot={}, execution_history_root=root)
    monkeypatch.setenv("OMNIBCI_CONFIG_DIR", str(tmp_path / "redirected"))
    worker._run_processor()
    recorder = processor_type.call_args.kwargs["record_execution"]
    assert isinstance(recorder.__self__, PluginExecutionHistory)
    assert recorder.__self__.directory == root / "execution-history"
    assert not recorder.__self__.directory.exists()
    processor_type.return_value.run.assert_called_once_with()
