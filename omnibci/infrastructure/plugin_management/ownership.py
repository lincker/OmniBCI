"""Local store ownership and controller-owned immutable execution history."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import RLock

from omnibci.application.plugin_management.execution import PluginExecutionReceipt, fingerprint
from omnibci.application.plugin_management.packages import (
    PluginPackageConflictError,
    PluginPackageStorageError,
)
from omnibci.infrastructure.storage import atomic_write, file_lock


class PluginStoreOwner:
    def __init__(self, plugin_dir: Path, *, configuration_root: Path | None = None, role: str | None = None):
        if role not in (None, "stimulation", "actuation"):
            raise ValueError("Unsupported plugin store owner role")
        if role is None and configuration_root is None:
            raise ValueError("A controller requires its configuration directory")
        self._guard = RLock()
        self._locks = ExitStack()
        self._pid = os.getpid()
        self._closed = True
        state = Path(plugin_dir).resolve() / "state"
        self._state = state
        self._role = role
        name = role or "controller"
        self._marker = state / f".{name}.active"
        try:
            paths = [state, state / "controller.json", self._marker]
            paths += [state / f".{scope}.lock" for scope in ("controller", name)]
            paths += [state / ".controller.active"]
            if any(path.is_symlink() for path in paths):
                raise PluginPackageStorageError("Plugin ownership state must not contain symlinks")
            state.mkdir(parents=True, exist_ok=True)
            self._locks.enter_context(file_lock(state / f".{name}.lock", timeout_s=0))
            self._require_settled(self._marker)
            if role is None:
                self._bind_configuration(state / "controller.json", configuration_root)
            else:
                try:
                    with file_lock(state / ".controller.lock", timeout_s=0):
                        self._require_settled(state / ".controller.active")
                except TimeoutError:
                    # An active controller may serve this independent node.
                    pass
            with self._marker.open("x", encoding="utf-8") as stream:
                stream.write("Plugin resource settlement has not been confirmed.\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._closed = False
        except BaseException as exc:
            self._locks.close()
            if isinstance(exc, TimeoutError):
                raise PluginPackageConflictError("Another owner is using this plugin store") from exc
            if isinstance(exc, OSError):
                raise PluginPackageStorageError(f"Cannot acquire plugin store ownership: {exc}") from exc
            raise

    @staticmethod
    def _require_settled(marker):
        if marker.is_symlink():
            raise PluginPackageStorageError("Plugin ownership state must not contain symlinks")
        if marker.exists():
            raise PluginPackageConflictError(
                f"Plugin store has an unconfirmed shutdown. Stop its hosts and device operations "
                f"before explicitly removing {marker} to acknowledge recovery"
            )

    @contextmanager
    def mutation(self):
        """Keep the shared reference guard and exclude independent resource readers."""
        if self._role is not None:
            raise PluginPackageConflictError("A read-only node cannot mutate plugin inventory")
        with self, ExitStack() as readers:
            try:
                for role in ("stimulation", "actuation"):
                    readers.enter_context(file_lock(self._state / f".{role}.lock", timeout_s=0))
                    self._require_settled(self._state / f".{role}.active")
            except TimeoutError as exc:
                raise PluginPackageConflictError("A node owner still uses this plugin store") from exc
            except OSError as exc:
                raise PluginPackageStorageError(f"Cannot guard plugin store mutation: {exc}") from exc
            yield

    @staticmethod
    def _bind_configuration(path, root):
        expected = {"schema_version": 1, "configuration_root": str(Path(root).resolve())}
        if path.exists():
            try:
                if path.stat().st_size > 16384:
                    raise ValueError("Ownership record is too large")
                actual = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise PluginPackageStorageError("Cannot read plugin reference ownership") from exc
            if actual != expected:
                raise PluginPackageConflictError("This plugin store belongs to another configuration directory")
        else:
            atomic_write(path, json.dumps(expected, sort_keys=True) + "\n")

    def __enter__(self):
        self._guard.acquire()
        if self._closed or os.getpid() != self._pid:
            self._guard.release()
            raise PluginPackageConflictError("Plugin store owner is closed or belongs to another process")
        return self

    def __exit__(self, *_):
        self._guard.release()

    def close(self):
        """Release only after request, operation and resource owners settle."""
        with self._guard:
            if self._closed:
                return
            with self:
                self._marker.unlink()
                self._closed = True
                self._locks.close()

    def abandon(self):
        """Release process locks while retaining the unconfirmed resource marker."""
        with self._guard:
            self._closed = True
            self._locks.close()


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
                record = {
                    "format_version": 1,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "receipt": receipt.to_dict(),
                }
                atomic_write(path, json.dumps(record, indent=2, allow_nan=False) + "\n")
