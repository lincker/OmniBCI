"""Output module edits are independent of processing configuration boundaries."""
from copy import deepcopy

import pytest

from tests.neuroengine.test_pipeline_configuration import selected_snapshot, _build
from tests.neuroengine.test_processing_configuration_boundary import configured, _stage, _results, _feed


def test_output_changes_do_not_invalidate_processing_replacement():
    before = selected_snapshot()
    after = deepcopy(before)
    after["CONFIG.module_revisions"].update(processing="new", stimulation="new-stimulation", actuation="new-actuation")
    after.update({"STIMULATION.renderer": "another-renderer", "ACTUATION.max_command_age_s": 2.0,
                  "ACQUISITION.trigger": "another-trigger"})
    after["PLUGINS.bindings"][1]["parameters"]["window_duration"] = 1.5
    after["PLUGINS.bindings"].append({
        "slot": "stimulation.paradigm", "kind": "paradigm", "profile_id": None,
        "selection": {"package_id": "remote.only", "contribution_id": "default", "version": "1.0.0"},
        "parameters": {}, "configuration_version": 1, "profile_revision": 0,
    })
    pipeline = _build(before, after)
    assert pipeline.decoder.SAMPLE_COUNT == 375
    assert before["CONFIG.module_revisions"]["processing"] == "old"


@pytest.mark.parametrize("field", ["CONFIG.engine_revision", "acquisition"])
def test_processing_replacement_rejects_changed_dependency(field):
    before = selected_snapshot()
    after = deepcopy(before)
    if field == "acquisition":
        after["CONFIG.module_revisions"][field] = "different-source"
    else:
        after[field] = "different-engine"
    with pytest.raises(ValueError, match="Source or Engine"):
        _build(before, after)


def test_module_boundary_preserves_result_epoch_and_source_sequence(configured):
    processor, events, replacement, builder = configured
    processor.completed_trials = 12
    processor._epoch_number = 17
    processor._last_sequence = 81
    _stage(processor, boundary="module")
    result, = _results(events)
    assert result.ok
    assert processor._decoder is replacement.decoder
    assert processor.processing_revision == "new"
    assert processor.acquisition_revision == "source-revision"
    assert (processor.completed_trials, processor._epoch_number, processor._last_sequence) == (12, 17, 81)
    assert builder.call_args.kwargs["boundary"] == "module"


@pytest.mark.asyncio
async def test_module_boundary_rejects_an_open_epoch_without_constructing_plugins(configured):
    processor, events, _, builder = configured
    await _feed(processor, [7, 0, 0])
    _stage(processor, boundary="module")
    result, = _results(events)
    assert not result.ok
    assert processor.processing_revision == "old"
    builder.assert_not_called()
