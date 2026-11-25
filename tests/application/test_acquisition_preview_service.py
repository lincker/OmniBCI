"""Preview use cases preserve worker identity and separate diagnostic delivery."""
import asyncio
from copy import deepcopy
from threading import Event
from unittest.mock import MagicMock

import pytest

from omnibci.application.acquisition_preview import PreviewSelection, limits
from omnibci.application.commands.acquisition_commands import StopStreamCommand
from omnibci.application.events import CommandResult, StreamDrainReceipt
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.services.acquisition_service import AcquisitionAppService
from omnibci.application.services.command_result_service import CommandExecutionError, CommandRejectedError
from omnibci.shared.events import EventType


def snapshot(**changes):
    return {"capture_id": "capture", "generation": "generation", "acquisition_revision": "a" * 64, "engine_revision": "engine",
            "source_id": "source", "connection_id": "connection", "status": "finished", "reason": "chunk_limit",
            "selection": PreviewSelection().to_dict(), "observed": 32, "enqueued": 2, "dropped": 30,
            "oversized": 0, "enqueued_bytes": 256, "limits": limits(),
            "stream_chunk_count": 40, "stream_sample_count": 5120, "last_sequence": 39, **changes}


@pytest.fixture
def harness():
    executor = MagicMock()
    state = snapshot()
    executor.request.side_effect = lambda event, payload: CommandResult("command", event.name, True, data=deepcopy(state))
    cache = MagicMock(return_value={"available": True, "cache_errors": 0, "received": 1,
                                   "retained_bytes": 128, "chunks": []})
    generation = MagicMock(return_value="generation")
    service = AcquisitionAppService(executor, preview_provider=cache, generation_provider=generation)
    return service, executor, cache, generation, state


@pytest.mark.parametrize("continuous", [False, True])
def test_start_generates_capture_id_and_sends_pinned_selection(harness, continuous):
    service, executor, cache, _, state = harness
    def request(event, payload):
        state.update(capture_id=payload["capture_id"], status="capturing", reason=None)
        return CommandResult("start", event.name, True, data=deepcopy(state))
    executor.request.side_effect = request
    result = service.start_preview(generation="generation", expected_revision="a" * 64,
                                   connection_id="connection", selection={"rows": [2, 0], "sample_count": 2},
                                   continuous=continuous)
    event, payload = executor.request.call_args.args
    assert event is EventType.StartAcquisitionPreview
    assert payload["capture_id"] and payload["capture_id"] == result["capture_id"]
    assert payload["generation"] == "generation" and payload["connection_id"] == "connection"
    assert payload["expected_revision"] == "a" * 64
    assert payload["selection"] == PreviewSelection(rows=(2, 0), sample_count=2).to_dict()
    assert payload["continuous"] is continuous
    cache.assert_called_once_with(result["capture_id"])


@pytest.mark.parametrize("continuous", [1, "true", None])
def test_invalid_continuous_flag_never_dispatches(harness, continuous):
    service, executor, _, _, _ = harness
    with pytest.raises(ValueError):
        service.start_preview(generation="generation", expected_revision="a" * 64,
                              connection_id="connection", continuous=continuous)
    executor.request.assert_not_called()


@pytest.mark.parametrize("selection", [
    {"rows": [0, 0]}, {"rows": [True]}, {"rows": [-1]}, {"rows": list(range(17))},
    {"rows": [], "include_trigger": False}, {"sample_count": 513}, {"chunk_count": 33},
    {"sample_start": -1}, {"rows": list(range(16)), "sample_count": 512}, {"unknown": 1},
])
def test_invalid_selection_never_dispatches(harness, selection):
    service, executor, cache, _, _ = harness
    with pytest.raises(ValueError):
        service.start_preview(generation="generation", expected_revision="a" * 64,
                              connection_id="connection", selection=selection)
    executor.request.assert_not_called()
    cache.assert_not_called()


def test_worker_rejection_does_not_start_a_signal_stream(harness):
    service, executor, cache, _, _ = harness
    executor.request.side_effect = CommandRejectedError(CommandResult(
        "start", "StartAcquisitionPreview", False, message="Start the signal stream before capturing a preview"))
    with pytest.raises(CommandRejectedError):
        service.start_preview(generation="generation", expected_revision="a" * 64, connection_id="connection")
    assert [call.args[0] for call in executor.request.call_args_list] == [EventType.StartAcquisitionPreview]
    cache.assert_not_called()


def test_read_does_not_query_metadata_or_hide_pending_delivery(harness):
    service, executor, _, _, _ = harness
    result = service.get_preview(generation="generation")
    executor.request.assert_called_once_with(EventType.GetAcquisitionPreview, {"generation": "generation"})
    assert result["enqueued"] == 2 and result["received"] == 1
    assert "complete" not in result


def test_read_keeps_independent_counter_snapshots_and_capture_time_identity(harness):
    service, _, cache, _, state = harness
    state.update(status="capturing", connection_id="old-connection", acquisition_revision="b" * 64)
    cache.return_value["received"] = 3
    service._connection_id = "new-connection"
    result = service.get_preview(generation="generation", capture_id="capture")
    assert result["received"] == 3 and result["enqueued"] == 2
    assert result["connection_id"] == "old-connection" and result["acquisition_revision"] == "b" * 64


def test_stale_generation_is_rejected_before_dispatch(harness):
    service, executor, cache, _, _ = harness
    with pytest.raises(ProfileRevisionConflict):
        service.get_preview(generation="old-generation")
    executor.request.assert_not_called()
    cache.assert_not_called()


@pytest.mark.parametrize("stage", ["worker", "cache"])
def test_generation_change_during_read_is_rejected(harness, stage):
    service, _, _, generation, _ = harness
    generation.side_effect = ["generation", "new-generation"] if stage == "worker" else ["generation", "generation", "new-generation"]
    with pytest.raises(ProfileRevisionConflict):
        service.get_preview(generation="generation")


def test_worker_capture_mismatch_never_reads_cache(harness):
    service, _, cache, _, _ = harness
    with pytest.raises(ProfileRevisionConflict):
        service.get_preview(generation="generation", capture_id="old-capture")
    cache.assert_not_called()


def test_missing_chunk_is_a_lookup_error(harness):
    service, _, cache, _, _ = harness
    cache.return_value["chunk"] = None
    with pytest.raises(LookupError):
        service.get_preview_chunk(5, generation="generation", capture_id="capture")
    cache.assert_called_once_with("capture", 5)


def test_detail_preserves_selected_values_and_pinned_descriptor(harness):
    service, _, cache, _, state = harness
    detail = {key: state[key] for key in ("capture_id", "generation", "acquisition_revision", "engine_revision", "source_id", "connection_id")}
    detail.update(sequence_id=5, timestamp_ns="1780000000000000001", original_shape=[3, 4], dtype="float64",
                  row_indices=[1, 2], sample_start=1, sample_count=3, trigger_row_index=2,
                  values=[[1.5, "NaN", "Infinity"], ["9007199254740993", "-Infinity", True]], descriptor=None)
    cache.return_value["chunk"] = detail
    result = service.get_preview_chunk(5, generation="generation", capture_id="capture")
    assert result == detail
    result["values"][0][0] = 0
    assert detail["values"][0][0] == 1.5


def test_cache_cannot_return_a_chunk_from_another_capture(harness):
    service, _, cache, _, _ = harness
    cache.return_value["chunk"] = {"capture_id": "other", "generation": "generation"}
    with pytest.raises(ProfileRevisionConflict):
        service.get_preview_chunk(5, generation="generation", capture_id="capture")


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [False, True])
@pytest.mark.parametrize("action", ["start", "stop"])
async def test_inflight_preview_read_does_not_reject_source_controls(harness, engine_service, detail, action):
    service, executor, cache, _, state = harness
    service._admission = engine_service.admission
    service._connected, service._connection_id = True, "connection"
    service._descriptor = MagicMock(sample_rate=250, signal_channel_count=1)
    service._raw_metadata = {"source_id": "source", "descriptor": {"provenance": "protocol-defined"}}
    engine_service._acquisition = service
    engine_service._running = engine_service._accepting_commands = True
    engine_service._engine_thread = MagicMock()
    engine_service._engine_thread.is_alive.return_value = True
    engine_service._applied_snapshot = {
        "CONFIG.generation": "generation", "CONFIG.module_revisions": {"acquisition": "a" * 64},
    }
    cache.return_value["chunk"] = {**state, "sequence_id": 5}
    entered, released = Event(), Event()
    events = []

    def request(event, payload=None, **kwargs):
        events.append(event)
        if event is EventType.GetAcquisitionPreview:
            entered.set()
            assert released.wait(5)
            data = deepcopy(state)
        else:
            data = StreamDrainReceipt(None, None, -1, 0, False).to_dict()
        return CommandResult("command", event.name, True, data=data)

    executor.request.side_effect = request
    read = (lambda: service.get_preview_chunk(5, generation="generation", capture_id="capture")) if detail else (
        lambda: service.get_preview(generation="generation"))
    preview = asyncio.create_task(asyncio.to_thread(read))
    control = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        control = asyncio.create_task(
            engine_service.start_reading(expected_revision="a" * 64, generation="generation", connection_id="connection")
            if action == "start" else asyncio.to_thread(service.handle_stop_stream, StopStreamCommand())
        )
        await asyncio.sleep(0)
        assert not control.done(), "Source controls should wait for the bounded read, not reject it"
        assert events == [EventType.GetAcquisitionPreview]
    finally:
        released.set()
        await asyncio.wait_for(preview, 2)
        if control is not None:
            await asyncio.wait_for(control, 2)
    assert events == [EventType.GetAcquisitionPreview,
                      EventType.StartStream if action == "start" else EventType.StopAndDrain]


@pytest.mark.parametrize("detail", [False, True])
def test_preview_read_reports_worker_exit_without_returning_cached_data(harness, detail):
    service, executor, cache, _, _ = harness
    executor.request.side_effect = CommandExecutionError(CommandResult(
        "command", "GetAcquisitionPreview", False,
        message="Module stopped before confirming command execution", error_code="MODULE_STOPPED",
    ))
    with pytest.raises(CommandExecutionError, match="Module stopped"):
        if detail:
            service.get_preview_chunk(5, generation="generation", capture_id="capture")
        else:
            service.get_preview(generation="generation")
    cache.assert_not_called()
