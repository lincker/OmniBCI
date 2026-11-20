"""Private execution evidence shared by host readiness and runtime history."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

from omnibci.application.configuration.definitions import MODULES
from omnibci.application.configuration.models import ProfileBinding, module_for_slot


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def bindings_fingerprint(bindings: list[dict], module: str) -> str:
    """Bind admission to exact selections, parameters and configuration versions."""
    selected = [ProfileBinding.from_dict(value) for value in bindings
                if value["slot"].split(".", 1)[0] == module]
    if not selected or len({binding.slot for binding in selected}) != len(selected):
        raise ValueError("Execution requires distinct owned plugin bindings")
    return fingerprint({binding.slot: fingerprint(binding.to_dict()) for binding in selected})


def _text(value, name):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"Execution receipt requires {name}")


def _sha256(value, name):
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise ValueError(f"Execution receipt requires a SHA-256 {name}")


@dataclass(frozen=True, slots=True, init=False)
class PluginExecutionReceipt:
    module: str
    operation_id: str
    generation: str
    module_revision: str
    engine_revision: str
    node_id: str
    target: str
    environment_sha256: str
    _artifacts_json: str = field(repr=False)

    def __init__(self, module: str, operation_id: str, generation: str,
                 module_revision: str, engine_revision: str, node_id: str, target: str,
                 environment_sha256: str, artifacts: dict):
        if module not in MODULES:
            raise ValueError("Unknown execution module")
        for name, value in (("operation_id", operation_id), ("generation", generation),
                            ("node_id", node_id), ("target", target)):
            _text(value, name)
        for name, value in (("module_revision", module_revision), ("engine_revision", engine_revision),
                            ("environment_sha256", environment_sha256)):
            _sha256(value, name)
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValueError("Execution receipt requires actual plugin artifacts")
        for slot, artifact in artifacts.items():
            if module_for_slot(slot) != module:
                raise ValueError("Execution receipt contains another module's artifact")
            if not isinstance(artifact, dict) or set(artifact) != {
                "binding_sha256", "content_sha256", "origin", "verified_repository",
            }:
                raise ValueError("Execution artifact has missing or unknown fields")
            for name in ("binding_sha256", "content_sha256"):
                _sha256(artifact[name], name)
            if artifact["origin"] not in {"builtin", "local", "market", "development"}:
                raise ValueError("Unknown execution artifact origin")
            if artifact["origin"] == "market":
                _text(artifact["verified_repository"], "verified_repository")
            elif artifact["verified_repository"] is not None:
                raise ValueError("Only verified market artifacts may name a repository")
        for name, value in (("module", module), ("operation_id", operation_id), ("generation", generation),
                            ("module_revision", module_revision), ("engine_revision", engine_revision),
                            ("node_id", node_id), ("target", target), ("environment_sha256", environment_sha256)):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_artifacts_json", json.dumps(artifacts, sort_keys=True, separators=(",", ":")))

    @property
    def artifacts(self) -> dict:
        return json.loads(self._artifacts_json)

    @property
    def bindings_sha256(self) -> str:
        return fingerprint({slot: artifact["binding_sha256"] for slot, artifact in self.artifacts.items()})

    def to_dict(self) -> dict:
        return {"module": self.module, "operation_id": self.operation_id, "generation": self.generation,
                "module_revision": self.module_revision, "engine_revision": self.engine_revision,
                "node_id": self.node_id, "target": self.target, "environment_sha256": self.environment_sha256,
                "artifacts": self.artifacts}

    @classmethod
    def from_dict(cls, data: dict) -> PluginExecutionReceipt:
        if not isinstance(data, dict) or set(data) != {
            "module", "operation_id", "generation", "module_revision", "engine_revision",
            "node_id", "target", "environment_sha256", "artifacts",
        }:
            raise ValueError("Execution receipt has missing or unknown fields")
        return cls(**data)
