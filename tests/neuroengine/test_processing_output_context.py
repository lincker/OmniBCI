"""Output metadata changes preserve the live pipeline and session identity."""
from copy import deepcopy
from queue import Queue
from types import SimpleNamespace

import pytest

from omnibci.application.events import CommandRequest
from omnibci.application.plugin_profiles import ProfileBinding
from omnibci.infrastructure.ipc.data_manager import DataManager
from omnibci.infrastructure.neuroengine.processing.pipeline_configuration import validate_output_context
from omnibci.infrastructure.neuroengine.processing.signal_processor import SignalProcessor, SignalProcessorCommand
from omnibci.infrastructure.plugins.plugin_catalog import PluginCatalog
from omnibci.shared.events import EventType
from tests.neuroengine.test_pipeline_configuration import selected_snapshot
from tests.neuroengine.test_processing_configuration_boundary import Add, Markers, ValueDecoder


@pytest.fixture
def context_update():
    current = selected_snapshot()
    candidate = deepcopy(current)
    candidate["CONFIG.revision"] = "new"
    candidate["ACTUATION.effector"] = "logger"
    candidate["PLUGINS.bindings"].append(ProfileBinding(
        "actuation.effector", "effector", None,
        PluginCatalog().resolve_legacy("effector", "logger").selection, {},
    ).to_dict())
    events = []
    processor = SignalProcessor(
        detector=Markers(), decoder=ValueDecoder(), filters=[Add(1)],
        command_queue=Queue(), data_manager=DataManager(buffer=Queue()),
        event_bus=SimpleNamespace(put_event=events.append), sample_rate=250,
        baseline_duration=.2, generation="run", config_revision="old", settings_snapshot=current,
    )
    return processor, current, candidate, events


def update(processor, candidate, **overrides):
    payload = {"settings_snapshot": candidate, "generation": "run", "expected_revision": "old"}
    payload.update(overrides)
    processor._execute((SignalProcessorCommand.update_context, CommandRequest("context-update", payload)))


def result(events):
    replies = [event.data for event in events if event.type is EventType.CommandResult]
    assert len(replies) == 1
    return replies[0]


def test_confirmed_output_context_keeps_objects_and_updates_next_epoch_revision(context_update):
    processor, before, candidate, events = context_update
    decoder, filters, detector, session = processor._decoder, processor._filters, processor.detector, processor.session
    update(processor, candidate)
    receipt = result(events)
    assert receipt.ok
    assert receipt.data == {"status": "applied", "command_id": "context-update",
                            "applied_revision": "new", "generation": "run"}
    assert processor._decoder is decoder
    assert processor._filters is filters
    assert processor.detector is detector
    assert processor.session is session
    assert processor.session.config_revision == "old"
    assert processor.new_epoch_processor().config_revision == "new"
    candidate["PLUGINS.bindings"][-1]["parameters"]["mutated"] = True
    assert processor._settings_snapshot["PLUGINS.bindings"][-1]["parameters"] == {}
    assert before["CONFIG.revision"] == "old"


@pytest.mark.parametrize("busy", ["epoch", "pending"])
def test_output_context_rejects_unfinished_epoch_or_staged_pipeline(context_update, busy):
    processor, _, candidate, events = context_update
    if busy == "epoch":
        processor.current_epoch = processor.new_epoch_processor()
    else:
        processor._pending_configuration = ("staged", object())
    update(processor, candidate)
    assert not result(events).ok
    assert processor.config_revision == "old"


@pytest.mark.parametrize("overrides", [{"generation": "another-run"}, {"expected_revision": "another-revision"}])
def test_stale_output_context_is_rejected_before_update(context_update, overrides):
    processor, _, candidate, events = context_update
    update(processor, candidate, **overrides)
    assert not result(events).ok
    assert processor.config_revision == "old"


@pytest.mark.parametrize("changed_slot", [0, 1, 2])
def test_output_context_cannot_smuggle_source_decoder_or_detector_changes(context_update, changed_slot):
    processor, _, candidate, events = context_update
    candidate["PLUGINS.bindings"][changed_slot]["parameters"]["changed"] = True
    update(processor, candidate)
    assert not result(events).ok
    assert processor.config_revision == "old"


@pytest.mark.parametrize("key,value", [
    ("ACQUISITION.sample_rate", 500), ("ALGORITHM.baseline_duration", .5),
    ("ACTUATION.enabled", True), ("CONFIG.generation", "other-run"),
])
def test_output_context_rejects_changed_host_settings(context_update, key, value):
    processor, before, candidate, events = context_update
    candidate[key] = value
    with pytest.raises(ValueError, match="Host context"):
        validate_output_context(before, candidate)
    update(processor, candidate)
    assert not result(events).ok
    assert processor.config_revision == "old"


def test_output_context_requires_pinned_bindings(context_update):
    _, before, candidate, _ = context_update
    candidate.pop("PLUGINS.bindings")
    with pytest.raises(ValueError, match="pinned bindings"):
        validate_output_context(before, candidate)
