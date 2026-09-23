"""Plan configuration changes against pinned, acknowledged runtime state."""
from collections import OrderedDict
import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from threading import RLock
from time import monotonic
from uuid import UUID, uuid4

from omnibci.application.plugin_profiles import ActivationSnapshot, ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError


@dataclass(frozen=True)
class ConfigurationPlan:
    plan_id: str
    expected_revision: str | None
    target_revision: str
    applied_revision: str | None
    generation: str | None
    running: bool
    strategy: str
    changed_slots: tuple[str, ...]
    changed_settings: tuple[str, ...]
    session_effect: str
    errors: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PlannedConfiguration:
    plan: ConfigurationPlan
    snapshot: ActivationSnapshot
    created_at: float


class RuntimeConfigurationService:
    """Keep review plans immutable even if a profile is edited before Apply."""

    _PLAN_TTL_S = 300
    _MAX_PLANS = 32

    def __init__(self, profiles, engine, *, executor=None):
        self.profiles = profiles
        self.engine = engine
        self.executor = executor
        self._plans: OrderedDict[str, PlannedConfiguration] = OrderedDict()
        self._operations: OrderedDict[str, dict] = OrderedDict()
        self._active_operation: str | None = None
        self._task: asyncio.Task | None = None
        self._lock = RLock()

    def status(self) -> dict:
        accepted = self.profiles.repository.get_activation()
        running = self.engine.is_engine_ready
        return {
            "running": running,
            "recovery_required": getattr(getattr(self.engine, "admission", None), "recovery_required", False) is True,
            "accepted_revision": accepted.revision if accepted else None,
            "applied_revision": self.engine.applied_revision if running else None,
            "generation": self.engine.generation if running else None,
            "operation": self.operation(self._active_operation) if self._active_operation else None,
        }

    def plan(self, bindings: list[dict], *, expected_revision: str | None,
             refresh_slots: list[str] | None = None,
             include_host_settings: bool = False) -> ConfigurationPlan:
        current = self.profiles.repository.get_activation()
        actual_revision = current.revision if current else None
        if expected_revision != actual_revision:
            raise ProfileRevisionConflict("The selected configuration changed; reload before planning")
        snapshot = self.profiles.propose(
            bindings, refresh_slots=refresh_slots,
            settings=self.profiles.settings_source() if include_host_settings else None,
        )
        running = self.engine.is_engine_ready
        errors = []
        try:
            self._validate_runtime(snapshot)
        except (ValueError, KeyError) as exc:
            errors.append(str(exc))
        required = {"acquisition.source", "processing.decoder", "processing.epoch_detector",
                    "stimulation.paradigm"}
        missing = required - {binding.slot for binding in snapshot.bindings}
        if missing:
            errors.append("Missing required selections: " + ", ".join(sorted(missing)))
        if snapshot.settings.get("ACTUATION.enabled", False) and not any(
            binding.slot == "actuation.effector" for binding in snapshot.bindings
        ):
            errors.append("Actuation is enabled without an effector selection")
        old = {binding.slot: binding for binding in current.bindings} if current else {}
        new = {binding.slot: binding for binding in snapshot.bindings}
        slots = sorted(slot for slot in old.keys() | new.keys()
                       if self._binding_values(old.get(slot)) != self._binding_values(new.get(slot)))
        old_order = [b.slot for b in current.bindings if b.kind == "filter"] if current else []
        new_order = [b.slot for b in snapshot.bindings if b.kind == "filter"]
        if old_order != new_order:
            slots = sorted(set(slots) | set(old_order) | set(new_order))
        old_settings = current.settings if current else {}
        settings = tuple(sorted(key for key in old_settings.keys() | snapshot.settings.keys()
                                if old_settings.get(key) != snapshot.settings.get(key)))
        strategy = self._strategy(slots, settings, old, new)
        if strategy == "no_change" and current is not None and snapshot.revision != current.revision:
            strategy = "rebuild"
        if self.executor is not None and strategy not in self.executor.supported_strategies:
            strategy = "rebuild"
        if not running:
            strategy = "select_on_start"
        elif current is None or self.engine.applied_revision != current.revision:
            errors.append("Applied and selected revisions differ; reconcile the runtime before applying")
        if running and strategy != "no_change":
            before = self.engine.applied_snapshot or {}
            # External peers have no negotiated configuration/restart protocol.
            # Reject before pausing or ending the active session.
            if strategy in ("rebuild", "module"):
                for module, affected in (
                    ("STIMULATION", strategy in ("rebuild", "module")),
                    ("ACTUATION", strategy in ("rebuild", "module")),
                ):
                    if not affected:
                        continue
                    enabled = module != "ACTUATION" or before.get("ACTUATION.enabled", False)
                    target_enabled = module != "ACTUATION" or snapshot.settings.get("ACTUATION.enabled", False)
                    if ((enabled and not before.get(f"{module}.auto_spawn", True)) or
                            (target_enabled and not snapshot.settings.get(f"{module}.auto_spawn", True))):
                        errors.append(f"{module.title()} is externally managed; runtime reconfiguration is unavailable")
        plan = ConfigurationPlan(
            str(uuid4()), actual_revision, snapshot.revision,
            self.engine.applied_revision if running else None,
            self.engine.generation if running else None, running, strategy,
            tuple(slots), settings,
            "continuation" if running and strategy == "rebuild" else "preserve",
            tuple(errors),
        )
        with self._lock:
            self._plans[plan.plan_id] = PlannedConfiguration(plan, snapshot, monotonic())
            while len(self._plans) > self._MAX_PLANS:
                self._plans.popitem(last=False)
        return plan

    def resolve_plan(self, plan_id: str) -> PlannedConfiguration:
        with self._lock:
            stored = self._plans.get(plan_id)
        if stored is None or monotonic() - stored.created_at > self._PLAN_TTL_S:
            raise ProfileRevisionConflict("Configuration plan expired; create a new plan")
        plan = stored.plan
        if plan.errors:
            raise PluginSchemaError("; ".join(plan.errors))
        current = self.profiles.repository.get_activation()
        revision = current.revision if current else None
        running = self.engine.is_engine_ready
        if (revision != plan.expected_revision or running != plan.running
                or (running and (self.engine.generation != plan.generation
                                 or self.engine.applied_revision != plan.applied_revision))):
            raise ProfileRevisionConflict("Runtime or selected configuration changed; create a new plan")
        # Recheck availability; installed packages may have changed since review.
        self._validate_runtime(stored.snapshot)
        return stored

    def _validate_runtime(self, snapshot):
        usage = getattr(self.executor, "active_kinds", None)
        self.profiles.validate(snapshot, require_available=True,
                              active_kinds=usage(snapshot) if callable(usage) else None)

    def operation(self, operation_id: str):
        with self._lock:
            if operation_id not in self._operations:
                raise KeyError(operation_id)
            return deepcopy(self._operations[operation_id])

    def submit(self, plan_id: str, operation_id: str):
        try:
            if str(UUID(operation_id)) != operation_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise PluginSchemaError("operation_id must be a canonical UUID") from exc
        with self._lock:
            if operation_id in self._operations:
                operation = self._operations[operation_id]
                if operation["plan_id"] != plan_id:
                    raise ProfileRevisionConflict("Operation ID was already used for a different plan")
                return deepcopy(operation)
            if getattr(getattr(self.engine, "admission", None), "recovery_required", False) is True:
                raise ProfileRevisionConflict("Resolve or stop the uncertain runtime before applying again")
            if self._active_operation is not None:
                active = self._operations[self._active_operation]
                if active["status"] in ("queued", "applying"):
                    raise ProfileRevisionConflict("A configuration operation is already in progress")
            stored = self.resolve_plan(plan_id)
            if self.executor is None:
                raise RuntimeError("Runtime configuration executor is unavailable")
            operation = dict(operation_id=operation_id, plan_id=plan_id,
                             target_revision=stored.plan.target_revision, status="queued", stage="queued",
                             error=None, rolled_back=False, result=None)
            self._operations[operation_id] = operation
            self._active_operation = operation_id
            while len(self._operations) > 32:
                self._operations.popitem(last=False)
            self._task = asyncio.create_task(self._execute(operation_id, stored))
            return deepcopy(operation)

    async def _execute(self, operation_id, stored):
        operation = self._operations[operation_id]

        def progress(stage):
            with self._lock:
                operation.update(status="applying", stage=stage)

        try:
            progress("validating")
            result = await self.executor.execute(stored, progress)
        except BaseException as exc:
            if getattr(exc, "recovery_required", False):
                self.engine.admission.quarantine(str(exc))
            with self._lock:
                operation.update(
                    status="recovery_required" if getattr(exc, "recovery_required", False) else "failed",
                    error=str(exc), rolled_back=getattr(exc, "rolled_back", False),
                )
        else:
            with self._lock:
                operation.update(status="succeeded", stage="complete", result=result)

    async def wait_for_idle(self):
        """Shutdown waits for the owned operation; HTTP disconnect never cancels it."""
        if self._task is not None:
            await asyncio.shield(self._task)

    @staticmethod
    def _binding_values(binding):
        if binding is None:
            return None
        return (binding.kind, binding.selection, binding.parameters, binding.configuration_version)

    def _strategy(self, slots, settings, old, new):
        if not slots and not settings:
            return "no_change"
        if settings or any(s in ("acquisition.source", "processing.epoch_detector") for s in slots):
            return "rebuild"
        if all(s == "processing.decoder" or s.startswith("processing.filter.") for s in slots):
            # Schema annotations are a permission to use a narrower boundary;
            # unknown or restart-scoped parameter changes require a rebuild.
            for slot in slots:
                previous, following = old.get(slot), new.get(slot)
                if previous is None or following is None or previous.selection != following.selection:
                    continue
                schema = self.profiles.catalog.get(following.kind, following.selection).schema or {}
                properties = schema.get("properties", {})
                for key in previous.parameters.keys() | following.parameters.keys():
                    if previous.parameters.get(key) != following.parameters.get(key):
                        if properties.get(key, {}).get("x-apply") != "trial":
                            return "rebuild"
            return "trial_boundary"
        if all(s.startswith(("stimulation.", "actuation.")) for s in slots):
            return "module"
        return "rebuild"
