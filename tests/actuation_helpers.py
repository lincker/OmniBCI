"""Explicit v1 actuation envelopes for host and transport tests."""
from copy import deepcopy
import time
from uuid import uuid4


def actuation_payload(parameters=None, *, command_id=None, issued_at=None, origin=None):
    return {
        "command_id": uuid4().hex if command_id is None else command_id,
        "issued_at": time.time() if issued_at is None else issued_at,
        "origin": deepcopy(origin) if origin is not None else {
            "generation": "test-run", "trial_number": 1,
            "processing_revision": "a" * 64, "acquisition_revision": "b" * 64,
            "forced": False, "produced_at": "2026-11-21T00:00:00+00:00",
        },
        "parameters": deepcopy(parameters) if parameters is not None else {"target_id": 1},
    }
