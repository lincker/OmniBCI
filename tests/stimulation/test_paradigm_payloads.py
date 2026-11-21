"""Classic40 selects operations from explicit JSON object payloads only."""
from unittest.mock import Mock

import pytest

from omnibci.infrastructure.plugins.paradigms.classic40SSVEP.ssvep import Classic40SSVEP_2


@pytest.fixture
def paradigm(tmp_path):
    instance = Classic40SSVEP_2(parameters={}, resource_root=tmp_path)
    instance.trigger = Mock()
    instance.trigger.clear.return_value = instance.trigger.send.return_value = None
    instance._text_view = Mock()
    return instance


@pytest.mark.parametrize('event_id', [0, 1, 40])
def test_explicit_trial_preserves_cue_or_free_run_timing(paradigm, event_id):
    assert paradigm.present_trial({'event_id': event_id}) is None
    assert paradigm.is_presenting()
    if event_id == 0:
        paradigm.trigger.clear.assert_called_once_with()
        paradigm.trigger.send.assert_called_once_with(0)
    else:
        assert not paradigm.trigger.mock_calls


@pytest.mark.parametrize('payload', [None, 1, {}, {'event_id': True}, {'event_id': -1},
                                     {'event_id': 41}, {'event_id': 1.0}, {'event_id': '1'},
                                     {'event_id': 0, 'unknown': True}])
def test_invalid_trial_never_falls_back_to_free_run(paradigm, payload):
    with pytest.raises(ValueError):
        paradigm.present_trial(payload)
    assert not paradigm.is_presenting()
    assert not paradigm.trigger.mock_calls


@pytest.mark.parametrize('payload', [None, 1, {}, {'target_id': 0}, {'target_id': True},
                                     {'target_id': 41}, {'target_id': 1.0}, {'target_id': '1'}])
def test_invalid_result_changes_neither_activity_nor_feedback(paradigm, payload):
    with pytest.raises(ValueError):
        paradigm.present_result(payload)
    assert not paradigm.is_presenting()
    assert not paradigm._text_view.mock_calls


def test_result_object_preserves_character_and_delete_feedback(paradigm):
    assert paradigm.present_result({'target_id': 1}) is None
    assert paradigm.is_presenting()
    paradigm._text_view.add_text.assert_called_once()
    assert paradigm.present_result({'target_id': 40}) is None
    paradigm._text_view.remove_last.assert_called_once_with()
