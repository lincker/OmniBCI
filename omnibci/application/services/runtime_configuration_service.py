"""Plan configuration changes against pinned, acknowledged runtime state."""
from collections import OrderedDict
import asyncio
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import StrEnum
from threading import RLock
from time import monotonic
from uuid import UUID, uuid4

from omnibci.application.configuration.definitions import MODULES, SECTIONS, ENGINE_SETTING_DEFAULTS
from omnibci.application.configuration.models import ActivationSet, ModuleActivation, EngineSettings, MODULE_KINDS
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.plugin_schema import PluginSchemaError
from omnibci.application.processing_source import processing_input
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.services.command_result_service import CommandDeliveryError, CommandTimeoutError


def read_configuration(repository, sections):
    """Read only the persisted inputs owned or consumed by this operation."""
    return ActivationSet({module: repository.get_module(module) for module in MODULES if module in sections},
                         repository.get_engine())


def operation_scopes(stored):
    plan = stored.plan
    if plan.module == "engine" or stored.engine is not None:
        return tuple(MODULES)
    if plan.module == "acquisition" and plan.running and plan.strategy not in ("select_on_start", "no_change"):
        return ("acquisition", "processing")
    return (plan.module,)


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


class ApplyStrategy(StrEnum):
    SELECT_ON_START = "select_on_start"
    NO_CHANGE = "no_change"
    REBUILD = "rebuild"
    TRIAL_BOUNDARY = "trial_boundary"
    MODULE = "module"


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
    strategy: ApplyStrategy
    changed_slots: tuple[str, ...]
    changed_settings: tuple[str, ...]
    errors: tuple[str, ...]
    signal_summary: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "strategy", ApplyStrategy(self.strategy))

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PlannedConfiguration:
    plan: ConfigurationPlan
    candidates: dict[str, ModuleActivation]
    engine: EngineSettings | None
    created_at: float
    restored_from: str | None = None

    def activation_set(self, current):
        return ActivationSet({**current.modules, **self.candidates},
                             self.engine if self.engine is not None else current.engine)


class RuntimeConfigurationService:
    """Keep review plans immutable even if a profile is edited before Apply."""

    _PLAN_TTL_S = 300
    _MAX_PLANS = 32

    def __init__(self, profiles, engine, *, executor=None, reference_guard=None):
        self.profiles = profiles
        self.engine = engine
        self.executor = executor
        self.reference_guard = reference_guard or nullcontext
        self._plans: OrderedDict[str, PlannedConfiguration] = OrderedDict()
        self._operations: OrderedDict[str, dict] = OrderedDict()
        self._active_operations: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending_packages: dict[str, tuple] = {}
        self._current_requests: dict[str, tuple] = {}
        self._lock = RLock()

    @property
    def _admission(self):
        return self.engine.admission

    @property
    def pending_package_snapshots(self):
        with self._lock:
            for identifier, (scopes, _) in tuple(self._pending_packages.items()):
                if identifier not in self._tasks and not self._admission.requires_recovery(scopes):
                    self._pending_packages.pop(identifier)
            return tuple(deepcopy(snapshot) for _, snapshot in self._pending_packages.values())

    def status(self) -> dict:
        accepted, errors = {}, {}
        for section in SECTIONS:
            try:
                value = (self.profiles.get_engine() if section == "engine"
                         else self.profiles.get_module(section))
                accepted[section] = value.revision if value is not None else None
            except (ValueError, KeyError, RuntimeError, OSError) as exc:
                accepted[section] = None
                errors[section] = str(exc)
        running = self.engine.is_engine_ready
        with self._lock:
            operations = {section: self.operation(self._active_operations[section])
                          if section in self._active_operations else None for section in SECTIONS}
        return {
            "running": running,
            "recovery": self._admission.recovery_status,
            "accepted": accepted,
            "applied": self.engine.applied_revisions if running else None,
            "generation": self.engine.generation if running else None,
            "operations": operations,
            "errors": errors,
        }

    def plan(self, module: str, bindings: list[dict], *, expected_revision: str | None,
             settings: dict | None = None, refresh_slots: list[str] | None = None) -> ConfigurationPlan:
        if module not in SECTIONS:
            raise PluginSchemaError("Unsupported configuration section")
        if module == "engine":
            if bindings or refresh_slots:
                raise PluginSchemaError("Engine settings cannot contain plugin bindings")
            if settings is None:
                raise PluginSchemaError("Current Engine settings are required")
            candidate = self.profiles.propose_engine(settings)
        else:
            if settings is None:
                raise PluginSchemaError("Current module settings are required")
            candidate = self.profiles.propose(module, bindings, settings=settings, refresh_slots=refresh_slots)
        return self._plan(module, candidate, expected_revision)

    def revert_plan(self, module: str, entry_id: str) -> ConfigurationPlan:
        if module not in MODULES:
            raise PluginSchemaError("Unsupported configuration module")
        entry = self.profiles.module_history_entry(module, entry_id)
        if entry is None:
            raise KeyError(entry_id)
        current = self.profiles.get_module(module)
        return self._plan(module, entry.activation, current.revision if current else None,
                          restored_from=entry_id)

    def _plan(self, module, candidate, expected_revision, *, restored_from=None):
        stored = self._prepare(module, candidate, expected_revision, restored_from=restored_from)
        with self._lock:
            self._plans[stored.plan.plan_id] = stored
            while len(self._plans) > self._MAX_PLANS:
                self._plans.popitem(last=False)
        return deepcopy(stored.plan)

    def _prepare(self, module, candidate, expected_revision, *, restored_from=None):
        dependencies = {module, "engine"}
        if module == "processing":
            dependencies.add("acquisition")
        if module == "acquisition" and self.engine.is_module_running("processing"):
            dependencies.add("processing")
        if module == "engine":
            dependencies.update(SECTIONS)
        current = self.profiles.selected_configuration(dependencies - {"engine"})
        revisions = current.revisions()
        if revisions[module] != expected_revision:
            raise ProfileRevisionConflict("The module configuration changed; reload before planning")
        candidates = {} if module == "engine" else {module: candidate}
        engine_settings = candidate if module == "engine" else None
        if engine_settings is None and current.engine is None:
            engine_settings = self.profiles.propose_engine(ENGINE_SETTING_DEFAULTS)
        combined = ActivationSet({**current.modules, **candidates}, engine_settings or current.engine)
        targets = {name: value.revision for name, value in candidates.items()}
        if engine_settings is not None:
            targets["engine"] = engine_settings.revision
        before = current.engine if module == "engine" else current.modules[module]
        old = {b.slot: b for b in before.bindings} if module != "engine" and before else {}
        new = {b.slot: b for b in candidate.bindings} if module != "engine" else {}
        slots = sorted(s for s in old.keys() | new.keys()
                       if self._binding_values(old.get(s)) != self._binding_values(new.get(s)))
        if [s for s in old if s.startswith("processing.filter.")] != [s for s in new if s.startswith("processing.filter.")]:
            slots = sorted(set(slots) | {s for s in old.keys() | new.keys() if s.startswith("processing.filter.")})
        old_settings = before.settings if before else {}
        changed_settings = tuple(sorted(k for k in old_settings.keys() | candidate.settings.keys()
                                        if old_settings.get(k) != candidate.settings.get(k)))
        changed = any(revisions[k] != v for k, v in targets.items())
        running = self.engine.is_engine_ready
        applied = self.engine.applied_revisions if running else None
        strategy = self._strategy(module, slots, changed_settings, old, new) if changed else "no_change"
        if changed and engine_settings is not None and engine_settings.revision != revisions["engine"]:
            strategy = "rebuild"
        if changed and (not running or module in ("acquisition", "processing")
                        and not self.engine.is_module_running(module)):
            strategy = "select_on_start"
        if (running and module != "engine" and not self.engine.is_module_running(module)
                and applied.get(module) != candidate.revision):
            strategy = "select_on_start" if module in ("acquisition", "processing") else "module"
        expected = {key: revisions[key] for key in SECTIONS if key in dependencies}
        errors = []
        try:
            self._validate_runtime(combined, candidates, strategy)
        except (ValueError, KeyError) as exc:
            errors.append(str(exc))
        if running and any(applied.get(key) != revision for key, revision in expected.items()
                           if revision is not None and (key == "engine" or self.engine.is_module_running(key))):
            errors.append("Applied and selected module revisions differ; reconcile the runtime before applying")
        projection = {"PLUGINS.bindings": [b.to_dict() for b in combined.bindings()]}
        plan = ConfigurationPlan(
            str(uuid4()), module, expected_revision, expected, targets,
            {key: applied.get(key) for key in expected} if applied is not None else None,
            self.engine.generation if running else None, running, strategy,
            tuple(slots), changed_settings, tuple(errors),
            self._signal_summary(projection) if module == "processing" else None,
        )
        return PlannedConfiguration(plan, candidates, engine_settings, monotonic(), restored_from)

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
        current = self.profiles.selected_configuration(set(plan.expected_revisions) - {"engine"})
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
        return deepcopy(stored)

    def _validate_runtime(self, activation_set, candidates, strategy):
        if not self.engine.is_engine_ready and self.engine.foundation_requires_cleanup:
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

    @staticmethod
    def _validate_operation_id(operation_id):
        try:
            if str(UUID(operation_id)) != operation_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise PluginSchemaError("operation_id must be a canonical UUID") from exc

    async def execute_current(self, module, action, *, operation_id, configuration):
        """Apply the submitted values and finish their action under one owner."""
        if module not in MODULES or action != ("connect" if module == "acquisition" else "start"):
            raise PluginSchemaError("Unsupported current configuration action")
        self._validate_operation_id(operation_id)
        request = module, action, deepcopy(configuration)
        with self.reference_guard(), self._lock:
            if operation_id in self._operations:
                if self._current_requests.get(operation_id) != request:
                    raise ProfileRevisionConflict("Operation ID was already used for a different request")
                task = self._tasks.get(operation_id)
            else:
                if self.executor is None:
                    raise RuntimeError("Runtime configuration executor is unavailable")
                if self._admission.requires_recovery(module):
                    raise ProfileRevisionConflict("Resolve or stop the uncertain runtime module before starting")
                start_foundation = module in ("acquisition", "processing") and not self.engine.is_engine_ready
                scopes = tuple(MODULES) if (start_foundation
                                           or self.profiles.get_engine() is None) else (module,)
                reservation = self._admission.reserve_transition(scopes, recovery=start_foundation)
                self._current_requests[operation_id] = request
                self._operations[operation_id] = dict(
                    operation_id=operation_id, plan_id=operation_id, module=module,
                    affected_modules=list(scopes), target_revisions={}, status="queued", stage="queued",
                    error=None, rolled_back=False, result=None,
                )
                self._active_operations[module] = operation_id
                self._pending_packages[operation_id] = scopes, {
                    "PLUGINS.bindings": deepcopy(configuration.get("bindings", ())),
                }
                try:
                    task = asyncio.create_task(self._execute_current(operation_id, module, action, request[2], scopes, reservation))
                    self._tasks[operation_id] = task
                except BaseException:
                    self._admission.release_reservation(reservation)
                    self._operations.pop(operation_id)
                    self._current_requests.pop(operation_id)
                    self._active_operations.pop(module)
                    self._pending_packages.pop(operation_id)
                    raise
                self._trim_operations()
        if task is not None:
            await asyncio.shield(task)
        return self.operation(operation_id)

    async def _execute_current(self, operation_id, module, action, configuration, scopes, reservation):
        operation = self._operations[operation_id]
        def progress(stage):
            with self._lock:
                operation.update(status="applying", stage=stage)
        try:
            with self._admission.transition(scopes, reservation=reservation):
                progress("validating")
                if not self.engine.is_engine_ready and module not in ("acquisition", "processing"):
                    raise RuntimeError("Start the engine before starting a module")
                candidate = self.profiles.propose(module, configuration["bindings"], settings=configuration["settings"])
                previous = self.profiles.get_module(module)
                if (previous.revision if previous else None) != configuration["expected_revision"]:
                    raise ProfileRevisionConflict("The module configuration changed; reload before starting")
                kinds = MODULE_KINDS[module] if candidate.settings.get(f"{module.upper()}.auto_spawn", True) else set()
                self.profiles.validate_module(candidate, active_kinds=kinds)
                required = {"acquisition": {"acquisition.source"},
                            "processing": {"processing.decoder", "processing.epoch_detector"},
                            "stimulation": {"stimulation.paradigm"}, "actuation": {"actuation.effector"}}[module]
                if required - {binding.slot for binding in candidate.bindings}:
                    raise PluginSchemaError("Missing required selections: " + ", ".join(sorted(required)))
                if module == "acquisition":
                    status = self.executor.acquisition.handle_get_status(GetAcquisitionStatusQuery())
                    if status.connected or status.streaming:
                        raise ProfileRevisionConflict("Stop reading and disconnect before connecting the source")
                    if (previous is None or previous.revision != candidate.revision) and self.engine.is_module_running(module):
                        await self.engine.stop_module_for_configuration(module)
                stored = self._prepare(module, candidate, configuration["expected_revision"])
                if stored.plan.errors:
                    raise PluginSchemaError("; ".join(stored.plan.errors))
                if not self._admission.owns(operation_scopes(stored)):
                    raise ProfileRevisionConflict("Runtime scope changed; retry the current action")
                with self._lock:
                    operation.update(plan_id=stored.plan.plan_id, target_revisions=stored.plan.target_revisions)
                    self._pending_packages[operation_id] = scopes, {
                        "PLUGINS.bindings": [binding.to_dict() for binding in candidate.bindings],
                    }
                result = await self.executor.execute(stored, progress, operation_id=operation_id)
                with self._lock:
                    operation["result"] = {**result, "configuration_applied": True}
                progress(action)
                outcome = await self.executor.execute_current_action(stored, action)
                with self._lock:
                    operation["result"]["action"] = outcome
            if module in ("acquisition", "processing"):
                await self.executor.prepare_processing()
        except BaseException as exc:
            unknown = isinstance(exc, CommandTimeoutError) or isinstance(exc, CommandDeliveryError) and exc.execution_unknown
            if unknown:
                self._admission.quarantine(str(exc), module)
            with self._lock:
                operation.update(status="recovery_required" if self._admission.requires_recovery(module)
                                 or getattr(exc, "recovery_required", False) else "failed",
                                 error=str(exc), rolled_back=getattr(exc, "rolled_back", False))
        else:
            with self._lock:
                operation.update(status="succeeded", stage="complete")
        finally:
            with self._lock:
                self._tasks.pop(operation_id, None)
                if operation["status"] != "recovery_required":
                    self._pending_packages.pop(operation_id, None)

    def _trim_operations(self):
        for identifier in tuple(self._operations):
            if len(self._operations) <= 32:
                break
            if identifier not in self._active_operations.values() and identifier not in self._tasks:
                self._operations.pop(identifier)
                self._current_requests.pop(identifier, None)

    def submit(self, plan_id: str, operation_id: str):
        self._validate_operation_id(operation_id)
        with self.reference_guard(), self._lock:
            if operation_id in self._operations:
                operation = self._operations[operation_id]
                if operation["plan_id"] != plan_id:
                    raise ProfileRevisionConflict("Operation ID was already used for a different plan")
                return deepcopy(operation)
            stored = self.resolve_plan(plan_id)
            scopes = operation_scopes(stored)
            if self._admission.requires_recovery(scopes):
                raise ProfileRevisionConflict("Resolve or stop the uncertain runtime modules before applying again")
            if self.executor is None:
                raise RuntimeError("Runtime configuration executor is unavailable")
            reservation = self._admission.reserve_transition(scopes)
            operation = dict(operation_id=operation_id, plan_id=plan_id,
                             module=stored.plan.module, affected_modules=list(scopes),
                             target_revisions=stored.plan.target_revisions, status="queued", stage="queued",
                             error=None, rolled_back=False, result=None)
            self._operations[operation_id] = operation
            self._active_operations[stored.plan.module] = operation_id
            self._pending_packages[operation_id] = scopes, {
                "PLUGINS.bindings": [binding.to_dict() for candidate in stored.candidates.values()
                                     for binding in candidate.bindings],
            }
            self._trim_operations()
            try:
                self._tasks[operation_id] = asyncio.create_task(self._execute(operation_id, stored, reservation))
            except BaseException:
                self._admission.release_reservation(reservation)
                self._operations.pop(operation_id)
                self._active_operations.pop(stored.plan.module)
                self._pending_packages.pop(operation_id)
                raise
            return deepcopy(operation)

    async def _execute(self, operation_id, stored, reservation):
        operation = self._operations[operation_id]

        def progress(stage):
            with self._lock:
                operation.update(status="applying", stage=stage)

        try:
            with self._admission.transition(operation_scopes(stored), reservation=reservation):
                progress("validating")
                result = await self.executor.execute(stored, progress, operation_id=operation_id)
        except BaseException as exc:
            with self._lock:
                operation.update(
                    status="recovery_required" if getattr(exc, "recovery_required", False) else "failed",
                    error=str(exc), rolled_back=getattr(exc, "rolled_back", False),
                )
        else:
            with self._lock:
                operation.update(status="succeeded", stage="complete", result=result)
        finally:
            with self._lock:
                self._tasks.pop(operation_id, None)
                if operation["status"] != "recovery_required":
                    self._pending_packages.pop(operation_id, None)

    async def wait_for_idle(self):
        """Shutdown waits for the owned operation; HTTP disconnect never cancels it."""
        while True:
            with self._lock:
                tasks = tuple(self._tasks.values())
            if not tasks:
                return
            await asyncio.shield(asyncio.gather(*tasks))

    @staticmethod
    def _binding_values(binding):
        if binding is None:
            return None
        return (binding.kind, binding.selection, binding.parameters, binding.configuration_version,
                binding.processing_input)

    def _strategy(self, module, slots, settings, old, new):
        if module in ("engine", "acquisition"):
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
            properties = (self.profiles.get_plugin_configuration(following.kind, following.selection).schema or {}).get("properties", {})
            for key in previous.parameters.keys() | following.parameters.keys():
                if previous.parameters.get(key) != following.parameters.get(key):
                    if properties.get(key, {}).get("x-apply") != "trial":
                        return "module"
        return "trial_boundary"
