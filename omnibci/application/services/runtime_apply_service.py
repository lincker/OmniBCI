"""Own pause/rebuild/restore as one admitted operation with explicit recovery."""
import asyncio
from dataclasses import dataclass, asdict

from omnibci.application.commands.acquisition_commands import ConnectDataProviderCommand, StartStreamCommand
from omnibci.application.commands.stimulation_commands import StartStimulationCommand
from omnibci.application.configuration.definitions import ENGINE_MODULES, OUTPUT_MODULES, merge_module_settings, module_runtime_settings
from omnibci.application.events import StreamDrainReceipt
from omnibci.application.configuration.models import ActivationSet
from omnibci.application.configuration.ports import ConfigurationRepository
from omnibci.application.configuration.errors import ProfileRevisionConflict
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.domain.ports.acquisition import SourceCheckpoint
from omnibci.application.source_checkpoint import checkpoint_for_restore, checkpoint_from_receipt
from omnibci.application.services.command_result_service import CommandRejectedError, CommandTimeoutError
from omnibci.application.services.runtime_configuration_service import (
    operation_scopes, plan_matches, read_configuration, validate_external_peers,
)
from omnibci.shared.events import EventType


class RuntimeApplyFailure(RuntimeError):
    def __init__(self, message, *, recovery_required=False, rolled_back=False):
        super().__init__(message)
        self.recovery_required = recovery_required
        self.rolled_back = rolled_back


@dataclass
class _RestoreState:
    connected: bool
    streaming: bool
    checkpoint: SourceCheckpoint | None = None
    acquisition: bool = True
    processing: bool = True


class RuntimeApplyCoordinator:
    def __init__(self, engine, profiles, acquisition, stimulation, commands, *,
                 repository: ConfigurationRepository, actuation=None,
                 signal_preparer=None, signal_detacher=None):
        self.engine = engine
        self.profiles = profiles
        self.repository = repository
        self.acquisition = acquisition
        self.stimulation = stimulation
        self.actuation = actuation
        self.commands = commands
        self.signal_preparer = signal_preparer
        self.signal_detacher = signal_detacher

    def active_kinds(self, activation_set):
        if activation_set.modules.get("stimulation") and activation_set.engine is None:
            return {"paradigm", "trigger"} if (self.stimulation.is_active and
                activation_set.settings().get("STIMULATION.auto_spawn", True)) else set()
        if activation_set.modules.get("actuation") and activation_set.engine is None:
            return {"effector"} if (self.actuation is not None and self.actuation.is_active
                and activation_set.settings().get("ACTUATION.auto_spawn", True)) else set()
        kinds = set()
        if self.engine.is_module_running("acquisition"):
            kinds.add("acquisition")
        if self.engine.is_module_running("processing"):
            kinds.update(("decoder", "epoch_detector", "filter"))
        return kinds

    async def execute_current_action(self, stored, action):
        """Finish the user action while its configuration reservation is held."""
        module = stored.plan.module
        if module in OUTPUT_MODULES:
            snapshot = self.profiles.materialize(ActivationSet(stored.candidates, None))
            if module == "actuation":
                return await self.actuation.start(snapshot)
            return await asyncio.to_thread(self.stimulation.handle_start_stimulation, StartStimulationCommand(),
                                           settings_snapshot=snapshot)
        engine_settings = self.repository.get_engine()
        if not self.engine.is_engine_ready:
            foundation = self.profiles.materialize(ActivationSet({}, engine_settings))
            async with self.engine.runtime_transition("engine"):
                await self.engine.start_for_configuration(foundation)
        selected = self.profiles.materialize(ActivationSet(stored.candidates, engine_settings), self.engine.generation)
        snapshot = merge_module_settings(self.engine.applied_snapshot or {}, selected, (module,))
        snapshot = self.engine.prepare_plugins(snapshot, (module,))
        if not self.engine.is_module_running(module):
            await self.engine.start_module_for_configuration(module, snapshot)
        self.engine.acknowledge_configuration(snapshot, modules=(module,))
        if action == "connect":
            await asyncio.to_thread(self.acquisition.handle_connect, ConnectDataProviderCommand(),
                                    prepare_processing=False)
            return "connected"
        return "started"

    async def prepare_processing(self):
        """Optional input attachment has its own scope after source ownership settles."""
        await asyncio.to_thread(self.acquisition.prepare_processing)

    async def execute(self, stored, progress, *, operation_id):
        plan = stored.plan
        async with self.engine.runtime_transition(operation_scopes(stored)):
            previous = read_configuration(self.repository, plan.expected_revisions)
            revisions = previous.revisions()
            if (any(revisions[k] != v for k, v in plan.expected_revisions.items())
                    or not plan_matches(plan, self.engine, self.stimulation, self.actuation)):
                raise ProfileRevisionConflict("Runtime changed before configuration application")
            if plan.module not in OUTPUT_MODULES and not plan.running and self.engine.foundation_requires_cleanup:
                raise ProfileRevisionConflict("The previous engine still owns resources; stop it before selecting configuration")
            combined = stored.activation_set(previous)
            validate_external_peers(combined, stored.candidates, plan.strategy, self.stimulation, self.actuation)
            if (plan.running and plan.module in ("acquisition", "processing") and plan.strategy != "no_change"
                    and self.engine.is_module_running(plan.module) == (plan.strategy == "select_on_start")):
                raise ProfileRevisionConflict("Module state changed before configuration application")
            for candidate in (combined.modules if plan.module == "engine" else stored.candidates).values():
                if candidate is not None:
                    self.profiles.validate_module(candidate, active_kinds=self.active_kinds(combined))
            if plan.strategy in ("select_on_start", "no_change"):
                progress("persisting")
                before = self.engine.applied_snapshot if plan.running and plan.strategy == "select_on_start" else None
                target = self._runtime_target(stored) if plan.running and plan.strategy == "select_on_start" else None
                written = self._persist(stored, previous)
                if plan.running and plan.strategy == "select_on_start":
                    try:
                        self.engine.acknowledge_configuration(target, modules=tuple(stored.candidates))
                    except Exception as exc:
                        try:
                            self._rollback_saved(stored, previous, written)
                            self.engine.acknowledge_configuration(before, modules=tuple(stored.candidates))
                        except Exception as recovery:
                            raise RuntimeApplyFailure(f"Configuration selection failed: {exc}; restoration failed: {recovery}",
                                                      recovery_required=True) from recovery
                        raise RuntimeApplyFailure(f"Configuration selection failed; previous values restored: {exc}",
                                                  rolled_back=True) from exc
                result = {"outcome": "selected" if plan.strategy == "select_on_start" else "no_change"}
            elif plan.strategy == "trial_boundary" or plan.module == "processing" and plan.strategy == "module":
                result = await self._apply_pipeline(stored, previous, progress)
            elif plan.module in OUTPUT_MODULES:
                result = await self._apply_output(stored, previous, progress)
            elif plan.module == "processing":
                result = await self._rebuild_processing(stored, previous, progress)
            elif plan.module == "acquisition":
                result = await self._rebuild_acquisition(stored, previous, progress)
            else:
                result = await self._rebuild(stored, previous, progress)
            if plan.strategy != "no_change":
                result.update(applied=True, history_saved=True)
                try:
                    for candidate in stored.candidates.values():
                        self.repository.record_module_history(
                            candidate, operation_id, restored_from=stored.restored_from)
                except Exception as exc:
                    result.update(history_saved=False,
                                  warning=f"Configuration applied, but its history could not be saved: {exc}")
            return result

    def _persist(self, stored, previous):
        written = []
        repository = self.repository
        self._check_selected(previous, stored.plan.expected_revisions)
        try:
            if stored.engine is not None:
                repository.save_engine(stored.engine, expected_revision=previous.revisions()["engine"])
                written.append("engine")
            for module in ("acquisition", "stimulation", "processing", "actuation"):
                if module in stored.candidates:
                    repository.save_module(stored.candidates[module], expected_revision=previous.revisions()[module])
                    written.append(module)
        except Exception as exc:
            try:
                self._rollback_saved(stored, previous, written)
            except Exception as recovery:
                raise RuntimeApplyFailure(f"Saving failed: {exc}; restoration failed: {recovery}",
                                          recovery_required=True) from recovery
            raise
        return written

    def _rollback_saved(self, stored, previous, written):
        repository = self.repository
        for module in reversed(written):
            target_revision = stored.plan.target_revisions[module]
            if module == "engine":
                if previous.engine is None:
                    repository.remove_engine(expected_revision=target_revision)
                else:
                    repository.save_engine(previous.engine, expected_revision=target_revision)
            elif previous.modules[module] is None:
                repository.remove_module(module, expected_revision=target_revision)
            else:
                repository.save_module(previous.modules[module], expected_revision=target_revision)

    def _check_selected(self, previous, keys):
        revisions = read_configuration(self.repository, keys).revisions()
        if any(revisions[key] != previous.revisions()[key] for key in keys):
            raise ProfileRevisionConflict("Selected module configuration was changed by another writer")

    def _runtime_target(self, stored):
        before = self.engine.applied_snapshot
        selected = self.profiles.materialize(ActivationSet(stored.candidates, stored.engine), self.engine.generation)
        return merge_module_settings(before, selected, stored.candidates)

    async def _stage_pipeline(self, snapshot, expected_revision, *, boundary="trial"):
        identity = {"generation": snapshot["CONFIG.generation"],
                    "engine_revision": snapshot["CONFIG.engine_revision"]}
        try:
            data = await self._request(EventType.StageProcessingConfiguration, {
                "settings_snapshot": module_runtime_settings(snapshot, "processing"), "expected_revision": expected_revision,
                "boundary": boundary, **identity,
            })
        except CommandTimeoutError as exc:
            try:
                data = await self._request(EventType.CancelProcessingConfiguration,
                                           {"command_id": exc.command_id, **identity})
            except Exception as reconciliation:
                raise RuntimeApplyFailure("Signal Processing update outcome could not be reconciled",
                                          recovery_required=True) from reconciliation
            if not isinstance(data, dict) or data.get("command_id") != exc.command_id:
                raise RuntimeApplyFailure("Signal Processing reconciliation command mismatch", recovery_required=True)
            if data.get("status") == "cancelled":
                if any(data.get(k) != v for k, v in identity.items()) or data.get("applied_revision") != expected_revision:
                    raise RuntimeApplyFailure("Signal Processing cancellation identity mismatch", recovery_required=True)
                raise RuntimeApplyFailure("Trial boundary was not reached; previous pipeline retained") from exc
        self._pipeline_receipt(data, snapshot)
        return data

    @staticmethod
    def _pipeline_receipt(data, snapshot):
        if (not isinstance(data, dict) or data.get("generation") != snapshot["CONFIG.generation"]
                or data.get("engine_revision") != snapshot["CONFIG.engine_revision"]
                or data.get("applied_revision") != snapshot["CONFIG.module_revisions"]["processing"]
                or data.get("status") != "applied"):
            raise RuntimeApplyFailure("Signal Processing revision was not acknowledged", recovery_required=True)

    async def _apply_pipeline(self, stored, previous, progress):
        before = self.engine.applied_snapshot
        target = self.engine.prepare_plugins(self._runtime_target(stored), ('processing',))
        boundary = "module" if stored.plan.strategy == "module" else "trial"
        if boundary == "module":
            progress("detaching_processing")
            try:
                await self._detach_pipeline(before)
            except Exception as exc:
                raise RuntimeApplyFailure(f"Processing boundary was not confirmed: {exc}",
                                          recovery_required=True) from exc
        progress("waiting_for_trial_boundary" if boundary == "trial" else "replacing_processing")
        try:
            await self._stage_pipeline(target, before["CONFIG.module_revisions"]["processing"], boundary=boundary)
        except Exception as exc:
            if boundary == "trial":
                raise
            if (isinstance(exc, CommandRejectedError)
                    or isinstance(exc, RuntimeApplyFailure) and not exc.recovery_required):
                try:
                    await self._prepare_pipeline()
                except Exception as recovery:
                    raise RuntimeApplyFailure(f"Processing update rejected; input recovery failed: {recovery}",
                                              recovery_required=True) from recovery
                raise RuntimeApplyFailure(f"Processing update rejected; previous pipeline retained: {exc}",
                                          rolled_back=True) from exc
            raise RuntimeApplyFailure(f"Processing update outcome is unknown: {exc}",
                                      recovery_required=True) from exc
        try:
            self.engine.acknowledge_configuration(target, modules=("processing",))
        except Exception as exc:
            raise RuntimeApplyFailure(f"Processing changed, but engine acknowledgement failed: {exc}",
                                      recovery_required=True) from exc
        written = []
        attaching = False
        try:
            progress("persisting")
            written = self._persist(stored, previous)
            if boundary == "module":
                attaching = True
                await self._prepare_pipeline()
        except Exception as exc:
            if attaching or getattr(exc, "recovery_required", False):
                raise RuntimeApplyFailure(f"Processing update requires recovery: {exc}", recovery_required=True) from exc
            progress("rolling_back_processing")
            try:
                self._rollback_saved(stored, previous, written)
                self._check_selected(previous, stored.plan.expected_revisions)
                await self._stage_pipeline(before, target["CONFIG.module_revisions"]["processing"], boundary=boundary)
                self.engine.acknowledge_configuration(before, modules=("processing",))
                if boundary == "module":
                    await self._prepare_pipeline()
            except Exception as recovery:
                raise RuntimeApplyFailure(f"Saving failed: {exc}; pipeline recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Apply failed; previous pipeline restored: {exc}", rolled_back=True) from exc
        return {"outcome": "applied", "boundary": boundary}

    async def _detach_pipeline(self, snapshot):
        if self.signal_detacher is not None:
            receipt = await asyncio.to_thread(self.signal_detacher)
            if receipt is None:
                return None
        else:
            if not self.engine.is_module_running("acquisition"):
                return None
            data = await self._request(EventType.DetachProcessingInput, self._identity(snapshot))
            receipt = StreamDrainReceipt.from_value(data)
        if (receipt.generation != snapshot["CONFIG.generation"]
                or receipt.engine_revision != snapshot["CONFIG.engine_revision"]
                or receipt.acquisition_revision != snapshot["CONFIG.module_revisions"]["acquisition"]
                or receipt.processing_revision not in (None, snapshot["CONFIG.module_revisions"]["processing"])):
            raise RuntimeError("Processing detach receipt belongs to a different configuration")
        return receipt

    async def _prepare_pipeline(self):
        if self.signal_preparer is not None:
            await asyncio.to_thread(self.signal_preparer)

    async def _apply_output(self, stored, previous, progress):
        role = stored.plan.module
        owner = self.stimulation if role == "stimulation" else self.actuation
        before = owner.applied_snapshot
        target = self.profiles.materialize(ActivationSet(stored.candidates, None))
        active = owner.is_active
        written = []

        async def restore(snapshot):
            if not snapshot.get(f"{role.upper()}.auto_spawn", True):
                await asyncio.to_thread(owner.prepare_external, snapshot)
            elif active:
                if role == "actuation":
                    await owner.start(snapshot)
                else:
                    await asyncio.to_thread(owner.handle_start_stimulation, StartStimulationCommand(),
                                            settings_snapshot=snapshot)

        async def release():
            if role == "actuation":
                await owner.release()
            else:
                await asyncio.to_thread(owner.stop)

        if role == "stimulation" and active:
            progress("pausing_output")
            try:
                response = await asyncio.to_thread(owner.request, EventType.PauseStimulation)
                self._receipt(response.data, before, "paused", "renderer_stopped", module=role)
            except Exception as exc:
                raise RuntimeApplyFailure(f"Stimulation pause unconfirmed: {exc}", recovery_required=True) from exc
        try:
            if role == "actuation" and active:
                progress("settling_actuation")
            await release()
        except Exception as exc:
            raise RuntimeApplyFailure(f"{role.title()} stop unconfirmed: {exc}", recovery_required=True) from exc
        try:
            progress("replacing_output")
            await restore(target)
            progress("persisting")
            written = self._persist(stored, previous)
        except Exception as exc:
            if self.engine.admission.requires_recovery(role) or getattr(exc, "recovery_required", False):
                raise RuntimeApplyFailure(str(exc), recovery_required=True) from exc
            try:
                await release()
                self._rollback_saved(stored, previous, written)
                self._check_selected(previous, stored.plan.expected_revisions)
                await restore(before)
            except Exception as recovery:
                raise RuntimeApplyFailure(f"{role.title()} restoration failed: {recovery}", recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Previous {role.title()} configuration restored: {exc}", rolled_back=True) from exc
        return {"outcome": "applied", "boundary": "module"}

    async def _request(self, event_type, payload=None, **kwargs):
        result = await asyncio.to_thread(self.commands.request, event_type, payload, **kwargs)
        return result.data

    @staticmethod
    def _identity(snapshot):
        return {"generation": snapshot.get("CONFIG.generation"),
                "engine_revision": snapshot.get("CONFIG.engine_revision"),
                "expected_revision": snapshot["CONFIG.module_revisions"]["acquisition"]}

    @staticmethod
    def _receipt(data, snapshot, *flags, module=None):
        if (not isinstance(data, dict) or data.get("generation") != snapshot.get("CONFIG.generation")
                or data.get("engine_revision") != snapshot.get("CONFIG.engine_revision")
                or module is not None and data.get("module_revision") != snapshot["CONFIG.module_revisions"][module]
                or any(data.get(flag) is not True for flag in flags)):
            raise RuntimeError("Worker did not acknowledge the requested safe boundary")
        return data

    async def _quiesce(self, before, state, progress):
        receipt = None
        if self.engine.is_module_running("acquisition"):
            progress("draining")
            receipt = await asyncio.to_thread(self.acquisition.handle_stop_and_drain)
        if receipt is not None and (receipt.generation != before.get("CONFIG.generation")
                or receipt.engine_revision != before.get("CONFIG.engine_revision")
                or receipt.acquisition_revision != before["CONFIG.module_revisions"]["acquisition"]
                or self.engine.is_module_running("processing")
                and receipt.processing_revision != before["CONFIG.module_revisions"]["processing"]):
            raise RuntimeError("Drain receipt belongs to a different configuration")
        if state.connected:
            data = await self._request(EventType.GetSourceCheckpoint, self._identity(before))
            if (not isinstance(data, dict) or data.get("generation") != before.get("CONFIG.generation")
                    or data.get("acquisition_revision") != before["CONFIG.module_revisions"]["acquisition"]
                    or data.get("engine_revision") != before.get("CONFIG.engine_revision")):
                raise RuntimeError("Source checkpoint belongs to a different configuration")
            state.checkpoint = checkpoint_from_receipt(data)
            if state.streaming and data["resume_mode"] == "restart-only":
                # A source must explicitly promise that a normal start does
                # not rewind. Otherwise retain the paused source and drivers.
                raise RuntimeApplyFailure(
                    "Automatic resume requires a checkpoint or declared live-source resume support. "
                    "The source remains paused; "
                    "stop the engine before applying this configuration.",
                    recovery_required=True,
                )
        return receipt

    async def _restore_ready(self, snapshot, state, checkpoint,
                             *, require_live_resume=False):
        await self.engine.start_for_configuration(snapshot)
        running = self.engine.applied_snapshot
        if state.acquisition:
            await self.engine.start_module_for_configuration("acquisition", running)
        if state.processing:
            await self.engine.start_module_for_configuration("processing", running)
        resumed_checkpoint = await self._restore_source(running, state, checkpoint,
                                                        require_live_resume=require_live_resume)
        if state.streaming and self.signal_preparer is not None:
            await asyncio.to_thread(self.signal_preparer)
        return resumed_checkpoint

    async def _restore_source(self, running, state, checkpoint, *, require_live_resume=False):
        resumed_checkpoint = False
        if state.connected:
            await asyncio.to_thread(self.acquisition.handle_connect, ConnectDataProviderCommand())
            if checkpoint is not None or require_live_resume:
                current = await self._request(EventType.GetSourceCheckpoint, self._identity(running))
                self._receipt(current, running)
                if current.get("acquisition_revision") != running["CONFIG.module_revisions"]["acquisition"]:
                    raise RuntimeError("Source resume receipt belongs to a different configuration")
                current_checkpoint = checkpoint_from_receipt(current)
                if require_live_resume and current["resume_mode"] != "live-current":
                    raise RuntimeError("Replacement source did not confirm safe live resumption")
            if checkpoint is not None:
                selected = checkpoint_for_restore(checkpoint, current_checkpoint)
                if selected is not None:
                    data = await self._request(EventType.RestoreSourceCheckpoint,
                                               {**self._identity(running), "checkpoint": asdict(selected)})
                    self._receipt(data, running, "restored")
                    resumed_checkpoint = True
        return resumed_checkpoint

    async def _resume_stream(self, state):
        if state.streaming:
            await asyncio.to_thread(self.acquisition.handle_start_stream, StartStreamCommand())

    async def _rebuild_acquisition(self, stored, previous, progress):
        module = "acquisition"
        before = self.engine.applied_snapshot
        target = self.engine.prepare_plugins(self._runtime_target(stored), (module,))
        source = self.acquisition.handle_get_status(GetAcquisitionStatusQuery())
        state = _RestoreState(source.connected, source.streaming)
        stopped = resumed = False
        written = []
        try:
            receipt = await self._quiesce(before, state, progress)
            progress("stopping")
            await self.engine.stop_module_for_configuration(module)
            stopped = True
            progress("starting")
            await self.engine.start_module_for_configuration(module, target)
            self.engine.acknowledge_configuration(target, modules=(module,))
            old_source = previous.modules[module].bindings[0].selection
            new_source = stored.candidates[module].bindings[0].selection
            same_source = (old_source.package_id, old_source.contribution_id) == (new_source.package_id, new_source.contribution_id)
            replay_resumed = await self._restore_source(
                target, state, state.checkpoint if same_source else None,
                require_live_resume=state.streaming and same_source and state.checkpoint is None)
            if state.streaming and self.signal_preparer is not None:
                await asyncio.to_thread(self.signal_preparer)
            progress("persisting")
            written = self._persist(stored, previous)
            progress("restoring")
            resumed = state.streaming
            await self._resume_stream(state)
            return {"outcome": "applied", "boundary": "module",
                    "interrupted_epoch": receipt.interrupted_epoch if receipt is not None else False,
                    "replay_resumed": replay_resumed}
        except BaseException as exc:
            if not stopped or resumed or getattr(exc, "recovery_required", False):
                raise RuntimeApplyFailure(f"Module update requires recovery: {exc}", recovery_required=True) from exc
            progress("rolling_back")
            try:
                await self.engine.stop_module_for_configuration(module)
                self._rollback_saved(stored, previous, written)
                self._check_selected(previous, stored.plan.expected_revisions)
                await self.engine.start_module_for_configuration(module, before)
                self.engine.acknowledge_configuration(before, modules=(module,))
                await self._restore_source(before, state, state.checkpoint,
                                           require_live_resume=state.streaming and state.checkpoint is None)
                if state.streaming and self.signal_preparer is not None:
                    await asyncio.to_thread(self.signal_preparer)
                await self._resume_stream(state)
            except BaseException as recovery:
                raise RuntimeApplyFailure(f"Module update failed: {exc}; recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Module update failed; previous module restored: {exc}", rolled_back=True) from exc

    async def _rebuild_processing(self, stored, previous, progress):
        before = self.engine.applied_snapshot
        target = self.engine.prepare_plugins(self._runtime_target(stored), ('processing',))
        stopped = attaching = False
        written = []
        try:
            progress("detaching_processing")
            receipt = await self._detach_pipeline(before)
            progress("stopping")
            await self.engine.stop_module_for_configuration("processing")
            stopped = True
            progress("starting")
            await self.engine.start_module_for_configuration("processing", target)
            self.engine.acknowledge_configuration(target, modules=("processing",))
            progress("persisting")
            written = self._persist(stored, previous)
            progress("restoring")
            attaching = True
            await self._prepare_pipeline()
            return {"outcome": "applied", "boundary": "module",
                    "interrupted_epoch": receipt.interrupted_epoch if receipt is not None else False,
                    "replay_resumed": False}
        except BaseException as exc:
            if not stopped or attaching or getattr(exc, "recovery_required", False):
                raise RuntimeApplyFailure(f"Processing update requires recovery: {exc}", recovery_required=True) from exc
            progress("rolling_back")
            try:
                await self.engine.stop_module_for_configuration("processing")
                self._rollback_saved(stored, previous, written)
                self._check_selected(previous, stored.plan.expected_revisions)
                await self.engine.start_module_for_configuration("processing", before)
                self.engine.acknowledge_configuration(before, modules=("processing",))
                await self._prepare_pipeline()
            except BaseException as recovery:
                raise RuntimeApplyFailure(f"Processing update failed: {exc}; recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Processing update failed; previous pipeline restored: {exc}",
                                      rolled_back=True) from exc

    async def _rebuild(self, stored, previous, progress):
        plan = stored.plan
        candidate = stored.activation_set(previous)
        before = self.engine.applied_snapshot
        target = self.profiles.materialize(candidate)
        source = self.acquisition.handle_get_status(GetAcquisitionStatusQuery())
        state = _RestoreState(source.connected, source.streaming,
                              acquisition=self.engine.is_module_running("acquisition"),
                              processing=self.engine.is_module_running("processing"))
        target = self.engine.prepare_plugins(target, tuple(module for module in ENGINE_MODULES if getattr(state, module)))
        stopped = False
        resumed = False
        written = []
        try:
            receipt = await self._quiesce(before, state, progress)
            progress("stopping")
            await self.engine.stop_for_configuration()
            stopped = True
            self.acquisition.reset()
            progress("starting")
            # The source validates resource identity independently of editable
            # parameters (a new chunk size must never rewind the same recording).
            old_source = next((b for b in previous.bindings() if b.slot == "acquisition.source"), None)
            new_source = next((b for b in candidate.bindings() if b.slot == "acquisition.source"), None)
            same_source = (old_source is not None and new_source is not None
                           and old_source.selection.package_id == new_source.selection.package_id
                           and old_source.selection.contribution_id == new_source.selection.contribution_id)
            replay_resumed = await self._restore_ready(
                target, state, state.checkpoint if same_source else None,
                require_live_resume=state.streaming and same_source and state.checkpoint is None,
            )
            progress("persisting")
            written = self._persist(stored, previous)
            progress("restoring")
            # From this point data may produce non-replayable physical output.
            # An unknown start result must never trigger a rewind of the source.
            resumed = state.streaming
            await self._resume_stream(state)
            return {"outcome": "applied",
                    "interrupted_epoch": receipt.interrupted_epoch if receipt is not None else False,
                    "replay_resumed": replay_resumed}
        except BaseException as exc:
            if not stopped or resumed or self.engine.restart_blocked:
                raise RuntimeApplyFailure(
                    f"Configuration update stopped at an unconfirmed boundary: {exc}. "
                    "Inspect runtime status before resuming; no data or physical output was replayed.",
                    recovery_required=True,
                ) from exc
            progress("rolling_back")
            try:
                await self.engine.stop_for_configuration()
                self.acquisition.reset()
                self._rollback_saved(stored, previous, written)
                self._check_selected(previous, plan.expected_revisions)
                await self._restore_ready(
                    before, state, state.checkpoint,
                    require_live_resume=state.streaming and state.checkpoint is None,
                )
                await self._resume_stream(state)
            except BaseException as recovery:
                raise RuntimeApplyFailure(f"Apply failed: {exc}; recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Apply failed; previous configuration restored: {exc}",
                                      rolled_back=True) from exc
