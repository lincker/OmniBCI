"""Controller-owned, immutable records of acknowledged plugin execution."""
from datetime import datetime, timezone
import json
from pathlib import Path
from threading import RLock

from omnibci.application.plugin_execution import PluginExecutionReceipt, fingerprint
from omnibci.infrastructure.repositories.configuration_storage import atomic_write, file_lock


class PluginExecutionHistory:
    def __init__(self, configuration_root: Path):
        self.directory = configuration_root.resolve() / "execution-history"
        self._lock = RLock()

    def record(self, receipt: PluginExecutionReceipt) -> None:
        # A digest keeps arbitrary operation/generation text out of filesystem paths.
        key = fingerprint([receipt.module, receipt.generation, receipt.operation_id])
        with self._lock:
            if self.directory.is_symlink():
                raise OSError("Execution history directory must not be a symlink")
            self.directory.mkdir(parents=True, exist_ok=True)
            with file_lock(self.directory / ".lock"):
                path = self.directory / f"{key}.json"
                if path.is_symlink():
                    raise OSError("Execution history record must not be a symlink")
                if path.exists():
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if saved["format_version"] != 1 or PluginExecutionReceipt.from_dict(saved["receipt"]) != receipt:
                        raise ValueError("Execution operation already records different artifacts")
                    return
                record = {"format_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
                          "receipt": receipt.to_dict()}
                atomic_write(path, json.dumps(record, indent=2, allow_nan=False) + "\n")
