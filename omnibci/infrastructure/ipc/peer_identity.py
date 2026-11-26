"""Identity of one configured peer in a particular runtime generation."""

from dataclasses import dataclass
from omnibci.application.plugin_management.identity import is_valid_sha256, valid_engine_revision

from omnibci.application.plugin_management.execution import PluginExecutionReceipt


@dataclass(frozen=True)
class PeerIdentity:
    role: str
    module_revision: str
    engine_revision: str | None
    generation: str
    operation_id: str
    bindings_sha256: str

    def __post_init__(self):
        if self.role not in {"stimulation", "actuation"}:
            raise ValueError("Unknown peer identity role")
        if (not is_valid_sha256(self.module_revision) or not is_valid_sha256(self.bindings_sha256)
                or not valid_engine_revision(self.role, self.engine_revision)):
            raise ValueError("Peer identity requires module and Engine revisions")
        if not isinstance(self.generation, str) or not self.generation.strip():
            raise ValueError("Peer identity requires a runtime generation")
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("Peer identity requires a preparation operation")

    def accepts(self, receipt: PluginExecutionReceipt) -> bool:
        return (receipt.module == self.role and receipt.module_revision == self.module_revision
                and receipt.engine_revision == self.engine_revision and receipt.generation == self.generation
                and receipt.operation_id == self.operation_id and receipt.bindings_sha256 == self.bindings_sha256)
