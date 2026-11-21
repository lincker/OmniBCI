"""Choose a resume checkpoint using resource identity, without interpreting state."""
from copy import deepcopy

from omnibci.domain.ports.acquisition import SourceCheckpoint


def checkpoint_for_restore(previous: SourceCheckpoint | None, current: SourceCheckpoint | None) -> SourceCheckpoint | None:
    """Resume the same resource, reject changed content, or begin a new resource."""
    if previous is None:
        return None
    if not isinstance(previous, SourceCheckpoint) or not isinstance(current, SourceCheckpoint):
        raise ValueError("A current and previous SourceCheckpoint are required")
    if previous.resource_id != current.resource_id:
        return None
    if previous.resource_revision != current.resource_revision:
        raise ValueError("Signal resource changed since the checkpoint; cannot resume safely")
    return deepcopy(previous)


def checkpoint_from_receipt(data: dict) -> SourceCheckpoint | None:
    """Validate the single current checkpoint wire shape at the host boundary."""
    mode = data.get("resume_mode")
    if mode not in ("restart-only", "live-current", "checkpoint"):
        raise ValueError("Source did not acknowledge a valid resume_mode")
    checkpoint = data.get("checkpoint")
    if mode == "checkpoint":
        if not isinstance(checkpoint, dict):
            raise ValueError("Source did not provide a resumable checkpoint")
        return SourceCheckpoint(**checkpoint)
    if checkpoint is not None:
        raise ValueError("Non-checkpoint source must return None")
    return None
