"""Resource identity controls restart compatibility, not playback parameters."""
import pytest

from omnibci.application.source_checkpoint import checkpoint_for_restore
from omnibci.domain.ports.acquisition import SourceCheckpoint


def checkpoint(resource="input", revision="content-1", cursor=100):
    return SourceCheckpoint(resource_id=resource, resource_revision=revision, state={"cursor": cursor})


def test_same_resource_and_revision_restores_old_cursor_with_independent_copy():
    previous = checkpoint(cursor=120)
    restored = checkpoint_for_restore(previous, checkpoint(cursor=0))
    assert restored == previous
    restored.state["cursor"] = 0
    assert previous.state["cursor"] == 120


def test_different_resource_begins_new_timeline():
    assert checkpoint_for_restore(checkpoint(), checkpoint(resource="different-input")) is None


def test_changed_content_at_same_resource_is_not_silently_rewound():
    with pytest.raises(ValueError, match="changed"):
        checkpoint_for_restore(checkpoint(), checkpoint(revision="content-2"))


@pytest.mark.parametrize("previous,current", [
    ({"cursor": 120}, checkpoint()), (checkpoint(cursor=120), {"cursor": 0}),
    ({"cursor": 120}, None),
])
def test_missing_identity_is_rejected(previous, current):
    with pytest.raises(ValueError):
        checkpoint_for_restore(previous, current)


def test_no_previous_checkpoint_needs_no_restore():
    assert checkpoint_for_restore(None, checkpoint()) is None


@pytest.mark.parametrize("identity", ["input", {}, {"resource_id": "input", "revision": 1}])
def test_invalid_identity_fails_closed(identity):
    with pytest.raises(ValueError, match="SourceCheckpoint"):
        checkpoint_for_restore(checkpoint(), {"source_identity": identity})
