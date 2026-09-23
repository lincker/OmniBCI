"""Choose a resume checkpoint using resource identity, without interpreting state."""
from copy import deepcopy


def checkpoint_for_restore(previous: dict | None, current: dict | None) -> dict | None:
    """Resume the same resource, reject changed content, or begin a new resource.

    Sources may expose ``source_identity`` with a stable ``resource_id`` and a
    content ``revision``. Without that optional envelope, pass the checkpoint
    through for the source's strict restore validation; never silently rewind.
    """
    if previous is None:
        return None
    before = _identity(previous)
    after = _identity(current)
    if before is None or after is None:
        return deepcopy(previous)
    if before["resource_id"] != after["resource_id"]:
        return None
    if before["revision"] != after["revision"]:
        raise ValueError("Signal resource changed since the checkpoint; cannot resume safely")
    return deepcopy(previous)


def _identity(checkpoint: dict | None) -> dict | None:
    if checkpoint is None:
        return None
    if not isinstance(checkpoint, dict):
        raise ValueError("Invalid source checkpoint")
    identity = checkpoint.get("source_identity")
    if identity is None:
        return None
    if not isinstance(identity, dict) or any(
        not isinstance(identity.get(key), str) or not identity[key]
        for key in ("resource_id", "revision")
    ):
        raise ValueError("Invalid checkpoint source identity")
    return identity
