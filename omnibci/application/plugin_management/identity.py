"""Exact plugin identity and validation rules shared across layers, without I/O."""
from __future__ import annotations

from dataclasses import dataclass
import re


PLUGIN_KINDS = frozenset({
    "acquisition", "decoder", "epoch_detector", "filter", "paradigm", "trigger", "effector",
})
PACKAGE_DOMAINS = frozenset({"acquisition", "processing", "stimulation", "actuation"})
SUPPORTED_PLATFORMS = frozenset({"darwin", "linux", "win32"})

IDENTIFIER_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)
_PACKAGE_ID_RE = re.compile(rf"{IDENTIFIER_PATTERN}\.{IDENTIFIER_PATTERN}")
_VERSION_RE = re.compile(r"[0-9][A-Za-z0-9._+-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def is_valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(_IDENTIFIER_RE.fullmatch(value))


def is_valid_package_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 255 and bool(_PACKAGE_ID_RE.fullmatch(value))


def is_valid_version(value: object) -> bool:
    return isinstance(value, str) and bool(_VERSION_RE.fullmatch(value)) and not value.endswith(".")


def is_valid_sha256(value: object, *, ignore_case: bool = False) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    candidate = value.lower() if ignore_case else value
    return bool(_SHA256_RE.fullmatch(candidate))


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

        if not is_valid_version(self.version):
            raise ValueError("version must be an exact package version")

    def to_dict(self) -> dict:
        return {"package_id": self.package_id, "contribution_id": self.contribution_id, "version": self.version}

    @classmethod
    def from_dict(cls, data: dict) -> PluginSelection:
        if not isinstance(data, dict) or set(data) != {"package_id", "contribution_id", "version"}:
            raise ValueError("Document has missing or unknown fields")
        return cls(data["package_id"], data["contribution_id"], data["version"])
