"""Synthetic pins for tests that replace plugin construction or network peers."""
from copy import deepcopy
from pathlib import Path

from omnibci.application.configuration.models import ProfileBinding
from omnibci.application.plugin_identity import PluginSelection
from omnibci.application.plugin_execution import fingerprint


def output_bindings():
    return [ProfileBinding(f"{role}.{kind}", kind, None,
                           PluginSelection(f"test.{kind}", "default", "1.0.0"), {}).to_dict()
            for role, kind in (("stimulation", "paradigm"), ("actuation", "effector"))]


def synthetic_execution_snapshot(snapshot):
    snapshot = deepcopy(snapshot)
    snapshot.setdefault("PLUGINS.bindings", output_bindings())
    snapshot.setdefault("CONFIG.operation_id", "prepare-1")
    snapshot["PLUGINS.artifacts"] = {
        binding["slot"]: {
            "location": str(Path(__file__).resolve().parent),
            "plugin_dir": str(Path(__file__).resolve().parent),
            "entrypoint": "driver:Plugin", "content_sha256": "e" * 64,
            "environment_sha256": "f" * 64, "node_id": "test-node", "target": "test-target",
            "binding_sha256": fingerprint(ProfileBinding.from_dict(binding).to_dict()),
            "origin": "local", "verified_repository": None,
        } for binding in snapshot["PLUGINS.bindings"]
    }
    return snapshot
