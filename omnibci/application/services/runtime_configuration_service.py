"""Plan configuration changes against pinned, acknowledged runtime state."""
from collections import OrderedDict
import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass
from threading import RLock
from time import monotonic
from uuid import UUID, uuid4

from omnibci.application.activation_settings import MODULES, SECTIONS
from omnibci.application.plugin_profiles import (
    ActivationSet, ModuleActivation, SystemSettings, ProfileRevisionConflict,
)
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.processing_source import processing_input


def validate_external_peers(engine, activation_set, candidates, strategy):
    if not engine.is_engine_ready or strategy == "no_change":
        return
    active = engine.applied_snapshot or {}
    target = activation_set.settings()
    for role in ("stimulation", "actuation"):
        if role not in candidates and not (strategy == "rebuild" and not candidates):
            continue
        key = role.upper()
        external = (not active.get(f"{key}.auto_spawn", True)
                    or not target.get(f"{key}.auto_spawn", True))
        if external and engine.is_peer_connected(role):
            raise PluginSchemaError(f"{role.title()} is externally managed; disconnect its node before changing configuration")


@dataclass(frozen=True)
class ConfigurationPlan:
    plan_id: str
    module: str
    expected_revision: str | None
    expected_revisions: dict[str, str | None]
    target_revisions: dict[str, str]
    applied_revisions: dict[str, str | None] | None
    generation: str | None
    running: bool
    strategy: str
    changed_slots: tuple[str, ...]
    changed_settings: tuple[str, ...]
    errors: tuple[str, ...]
    signal_summary: str | None = None

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PlannedConfiguration:
    plan: ConfigurationPlan
    candidates: dict[str, ModuleActivation]
    system: SystemSettings | None
    created_at: float
    restored_from: str | None = None

    def activation_set(self, current):
        return ActivationSet({**current.modules, **self.candidates},
                             self.system if self.system is not None else current.system)


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
        accepted = self.profiles.repository.get_activation_set()
        running = self.engine.is_engine_ready
        return {
            "running": running,
            "recovery_required": getattr(getattr(self.engine, "admission", None), "recovery_required", False) is True,
            "accepted": accepted.revisions(),
            "applied": self.engine.applied_revisions if running else None,
            "generation": self.engine.generation if running else None,
            "operation": self.operation(self._active_operation) if self._active_operation else None,
        }

    def plan(self, module: str, bindings: list[dict], *, expected_revision: str | None,
             refresh_slots: list[str] | None = None) -> ConfigurationPlan:
        if module not in SECTIONS:
            raise PluginSchemaError("Unsupported configuration section")
        if module == "system":
            if bindings or refresh_slots:
                raise PluginSchemaError("System settings cannot contain plugin bindings")
            candidate = self.profiles.propose_system()
        else:
            candidate = self.profiles.propose(module, bindings, refresh_slots=refresh_slots)
        return self._plan(module, candidate, expected_revision)

    def revert_plan(self, module: str, entry_id: str) -> ConfigurationPlan:
        if module not in MODULES:
            raise PluginSchemaError("Unsupported configuration module")
        entry = self.profiles.repository.get_module_history_entry(module, entry_id)
        if entry is None:
            raise KeyError(entry_id)
        current = self.profiles.repository.get_module(module)
        return self._plan(module, entry.activation, current.revision if current else None,
                          restored_from=entry_id)

    def _plan(self, module, candidate, expected_revision, *, restored_from=None):
        current = self.profiles.repository.get_activation_set()
        revisions = current.revisions()
        if revisions[module] != expected_revision:
            raise ProfileRevisionConflict("The module configuration changed; reload before planning")
        candidates = {} if module == "system" else {module: candidate}
        system = candidate if module == "system" else None
        if system is None and current.system is None:
            system = self.profiles.propose_system()
        combined = ActivationSet({**current.modules, **candidates}, system or current.system)
        targets = {name: value.revision for name, value in candidates.items()}
        if system is not None:
            targets["system"] = system.revision
        before = current.system if module == "system" else current.modules[module]
        old = {b.slot: b for b in before.bindings} if module != "system" and before else {}
        new = {b.slot: b for b in candidate.bindings} if module != "system" else {}
        slots = sorted(s for s in old.keys() | new.keys()
                       if self._binding_values(old.get(s)) != self._binding_values(new.get(s)))
        if [s for s in old if s.startswith("processing.filter.")] != [s for s in new if s.startswith("processing.filter.")]:
            slots = sorted(set(slots) | {s for s in old.keys() | new.keys() if s.startswith("processing.filter.")})
        old_settings = before.settings if before else {}
        changed_settings = tuple(sorted(k for k in old_settings.keys() | candidate.settings.keys()
                                        if old_settings.get(k) != candidate.settings.get(k)))
        changed = any(revisions[k] != v for k, v in targets.items())
        running = self.engine.is_engine_ready
        strategy = self._strategy(module, slots, changed_settings, old, new) if changed else "no_change"
        if changed and system is not None and system.revision != revisions["system"]:
            strategy = "rebuild"
        if changed and (not running or module in ("acquisition", "processing")
                        and not self.engine.is_module_running(module)):
            strategy = "select_on_start"
        dependencies = {module, "system"}
        if module == "processing":
            dependencies.add("acquisition")
        if strategy == "rebuild" and module == "system":
            dependencies.update(SECTIONS)
        expected = {key: revisions[key] for key in SECTIONS if key in dependencies}
        applied = self.engine.applied_revisions if running else None
        errors = []
        try:
            self._validate_runtime(combined, candidates, strategy)
        except (ValueError, KeyError) as exc:
            errors.append(str(exc))
        if running and any(applied.get(key) != revision for key, revision in expected.items() if revision is not None):
            errors.append("Applied and selected module revisions differ; reconcile the runtime before applying")
        projection = {"PLUGINS.bindings": [b.to_dict() for b in combined.bindings()]}
        plan = ConfigurationPlan(
            str(uuid4()), module, expected_revision, expected, targets,
            {key: applied.get(key) for key in expected} if applied is not None else None,
            self.engine.generation if running else None, running, strategy,
            tuple(slots), changed_settings, tuple(errors),
            self._signal_summary(projection) if module == "processing" else None,
        )
        with self._lock:
            self._plans[plan.plan_id] = PlannedConfiguration(plan, candidates, system, monotonic(), restored_from)
            while len(self._plans) > self._MAX_PLANS:
                self._plans.popitem(last=False)
        return plan

    @staticmethod
    def _signal_summary(snapshot):
        try:
            value = processing_input(snapshot)
            if value is None:
                return None
            outputs = value.to_dict()["outputs"]
        except (ValueError, TypeError):
            return "Invalid channel mapping; correct the validation errors before applying"
        descriptions = []
        for item in outputs:
            source = item.get("source_channel_id")
            if source is None:
                source = f"row {item['source_channel_index'] + 1}"
            description = item["name"] if item["name"] == source else f"{source} → {item['name']}"
            if item.get("signal_type"):
                description += f" ({item['signal_type']})"
            descriptions.append(description)
        return "Channels: " + (", ".join(descriptions) or "all source channels")

    def resolve_plan(self, plan_id: str) -> PlannedConfiguration:
        with self._lock:
            stored = self._plans.get(plan_id)
        if stored is None or monotonic() - stored.created_at > self._PLAN_TTL_S:
            raise ProfileRevisionConflict("Configuration plan expired; create a new plan")
        plan = stored.plan
        if plan.errors:
            raise PluginSchemaError("; ".join(plan.errors))
        current = self.profiles.repository.get_activation_set()
        revisions = current.revisions()
        applied = self.engine.applied_revisions or {}
        if (any(revisions[k] != v for k, v in plan.expected_revisions.items())
                or self.engine.is_engine_ready != plan.running
                or (plan.running and (self.engine.generation != plan.generation
                    or any(applied.get(k) != v for k, v in plan.applied_revisions.items())))):
            raise ProfileRevisionConflict("Runtime or module configuration changed; create a new plan")
        self._validate_runtime(stored.activation_set(current), stored.candidates, plan.strategy)
        if (plan.running and plan.module in ("acquisition", "processing") and plan.strategy != "no_change"
                and self.engine.is_module_running(plan.module) == (plan.strategy == "select_on_start")):
            raise ProfileRevisionConflict("Module state changed; create a new plan")
        return stored

    def _validate_runtime(self, activation_set, candidates, strategy):
        if not self.engine.is_engine_ready and (
                getattr(self.engine, "restart_blocked", False)
                or getattr(self.engine, "runtime_package_snapshots", ())):
            raise PluginSchemaError("The previous engine still owns resources; stop it before selecting configuration")
        validate_external_peers(self.engine, activation_set, candidates, strategy)
        usage = getattr(self.executor, "active_kinds", None)
        active_kinds = usage(activation_set) if callable(usage) else None
        modules = activation_set.modules if strategy == "rebuild" and not candidates else candidates
        for candidate in modules.values():
            if candidate is not None:
                self.profiles.validate_module(candidate, active_kinds=active_kinds)
        if (self.engine.is_module_running("acquisition") and
                ("acquisition" in candidates or strategy == "rebuild" and not candidates)
                and not any(binding.slot == "acquisition.source" for binding in activation_set.bindings())):
            raise PluginSchemaError("The running Acquisition module requires acquisition.source")

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
                             target_revisions=stored.plan.target_revisions, status="queued", stage="queued",
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
            result = await self.executor.execute(stored, progress, operation_id=operation_id)
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
        return (binding.kind, binding.selection, binding.parameters, binding.configuration_version,
                binding.processing_input)

    def _strategy(self, module, slots, settings, old, new):
        if module in ("system", "acquisition"):
            return "rebuild"
        if module in ("stimulation", "actuation"):
            return "module"
        if settings or "processing.epoch_detector" in slots:
            return "rebuild"
        if (getattr(old.get("processing.decoder"), "processing_input", None)
                != getattr(new.get("processing.decoder"), "processing_input", None)):
            return "rebuild"
        if list(old) != list(new):
            return "module"
        for slot in slots:
            previous, following = old.get(slot), new.get(slot)
            if previous is None or following is None or previous.selection != following.selection:
                return "module"
            if previous.configuration_version != following.configuration_version:
                return "module"
            properties = (self.profiles.catalog.get(following.kind, following.selection).schema or {}).get("properties", {})
            for key in previous.parameters.keys() | following.parameters.keys():
                if previous.parameters.get(key) != following.parameters.get(key):
                    if properties.get(key, {}).get("x-apply") != "trial":
                        return "module"
        return "trial_boundary"
