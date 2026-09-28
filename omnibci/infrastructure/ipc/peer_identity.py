"""Identity of one configured peer in a particular engine generation."""

from dataclasses import dataclass, asdict
import re


@dataclass(frozen=True)
class PeerIdentity:
    role: str
    config_revision: str
    generation: str

    def __post_init__(self):
        if self.role not in {"stimulation", "actuation"}:
            raise ValueError("Unknown peer identity role")
        if not isinstance(self.config_revision, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.config_revision
        ):
            raise ValueError("Peer identity requires an activation revision")
        if not isinstance(self.generation, str) or not self.generation.strip():
            raise ValueError("Peer identity requires an engine generation")

    def to_dict(self) -> dict:
        return asdict(self)

    def matches(self, payload) -> bool:
        return isinstance(payload, dict) and payload == self.to_dict()
