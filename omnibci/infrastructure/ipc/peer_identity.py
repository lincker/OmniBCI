"""Identity of one configured peer in a particular engine generation."""

from dataclasses import dataclass
import re

from omnibci.application.plugin_management.execution import PluginExecutionReceipt


@dataclass(frozen=True)
class PeerIdentity:
    role: str
    module_revision: str
    engine_revision: str
    generation: str
    operation_id: str
    bindings_sha256: str

    def __post_init__(self):
        if self.role not in {"stimulation", "actuation"}:
            raise ValueError("Unknown peer identity role")
        for revision in (self.module_revision, self.engine_revision, self.bindings_sha256):
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
                raise ValueError("Peer identity requires module and Engine revisions")
        if not isinstance(self.generation, str) or not self.generation.strip():
            raise ValueError("Peer identity requires an engine generation")
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("Peer identity requires a preparation operation")

    def accepts(self, receipt: PluginExecutionReceipt) -> bool:
        return (receipt.module == self.role and receipt.module_revision == self.module_revision
                and receipt.engine_revision == self.engine_revision and receipt.generation == self.generation
                and receipt.operation_id == self.operation_id and receipt.bindings_sha256 == self.bindings_sha256)
