"""Exact plugin identity shared by application modules, without I/O."""
from __future__ import annotations

from dataclasses import dataclass
import re


PLUGIN_KINDS = frozenset({
    "acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector",
})


@dataclass(frozen=True, slots=True)
class PluginSelection:
    """Exact package, contribution and installed version identity."""

    package_id: str
    contribution_id: str
    version: str

    def __post_init__(self) -> None:
        for name in ("package_id", "contribution_id", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} must be a non-empty, trimmed string")

        if not re.fullmatch(r"[0-9][A-Za-z0-9._+-]{0,127}", self.version) or self.version.endswith("."):
            raise ValueError("version must be an exact package version")

    def to_dict(self) -> dict:
        return {"package_id": self.package_id, "contribution_id": self.contribution_id, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict) -> PluginSelection:
        if not isinstance(data, dict) or set(data) != {"package_id", "contribution_id", "version"}:
            raise ValueError("Document has missing or unknown fields")
        return cls(data["package_id"], data["contribution_id"], data["version"])
