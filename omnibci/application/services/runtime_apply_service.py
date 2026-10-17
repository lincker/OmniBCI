"""Own pause/rebuild/restore as one admitted operation with explicit recovery."""
import asyncio
from dataclasses import dataclass

from omnibci.application.commands.acquisition_commands import ConnectDataProviderCommand, StartStreamCommand
from omnibci.application.commands.session_commands import EndSessionCommand
from omnibci.application.commands.stimulation_commands import StartStimulationCommand, StopStimulationCommand
from omnibci.application.plugin_profiles import ProfileRevisionConflict
from omnibci.application.queries.acquisition_queries import GetAcquisitionStatusQuery
from omnibci.application.queries.session_queries import GetSessionTrialsQuery
from omnibci.application.source_checkpoint import checkpoint_for_restore
from omnibci.application.services.command_result_service import CommandTimeoutError
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
    stimulation: bool
    session_id: str | None
    checkpoint: dict | None = None


class RuntimeApplyCoordinator:
    supported_strategies = {"select_on_start", "no_change", "rebuild", "trial_boundary", "module"}

    def __init__(self, engine, profiles, acquisition, sessions, stimulation, commands, *, signal_preparer=None):
        self.engine = engine
        self.profiles = profiles
        self.acquisition = acquisition
        self.sessions = sessions
        self.stimulation = stimulation
        self.commands = commands
        self.signal_preparer = signal_preparer

    def active_kinds(self, snapshot):
        kinds = {"acquisition", "decoder", "epoch_detector", "filter"}
        owned = self.engine.runtime_package_snapshots
        if any(settings.get("ACTUATION.enabled", False) and settings.get("ACTUATION.auto_spawn", True)
               for settings in (snapshot.settings, *owned)):
            kinds.add("effector")
        if ((self.stimulation.is_active or self.stimulation.process_alive)
                and any(settings.get("STIMULATION.auto_spawn", True)
                        for settings in (snapshot.settings, *owned))):
            kinds.update(("paradigm", "trigger"))
        return kinds

    async def execute(self, stored, progress):
        plan, candidate = stored.plan, stored.snapshot
        async with self.engine.runtime_transition():
            previous = self.profiles.repository.get_activation()
            if ((previous.revision if previous else None) != plan.expected_revision
                    or self.engine.is_engine_ready != plan.running
                    or (plan.running and (self.engine.generation != plan.generation
                                          or self.engine.applied_revision != plan.applied_revision))):
                raise ProfileRevisionConflict("Runtime changed before configuration application")
            if not plan.running and (self.engine.restart_blocked or self.engine.runtime_package_snapshots):
                raise ProfileRevisionConflict(
                    "The previous engine still owns resources; stop it before selecting configuration"
                )
            self.profiles.validate_activation(candidate, active_kinds=self.active_kinds(candidate))
            if plan.strategy in ("select_on_start", "no_change"):
                progress("persisting")
                self.profiles.repository.save_activation(candidate, expected_revision=plan.expected_revision)
                return {"outcome": "selected" if not plan.running else "applied"}
            if plan.strategy not in self.supported_strategies:
                raise ValueError("The planned update boundary is unavailable")
            if plan.strategy == "trial_boundary":
                return await self._apply_pipeline(stored, previous, progress)
            if plan.strategy == "module":
                return await self._apply_outputs(stored, previous, progress)
            return await self._rebuild(stored, previous, progress)

    async def _stage_pipeline(self, snapshot, expected_revision):
        try:
            data = await self._request(EventType.StageProcessingConfiguration, {
                "settings_snapshot": snapshot, "expected_revision": expected_revision,
                "generation": snapshot["CONFIG.generation"],
            })
        except CommandTimeoutError as exc:
            try:
                data = await self._request(EventType.CancelProcessingConfiguration, {
                    "command_id": exc.command_id, "generation": snapshot["CONFIG.generation"],
                })
            except Exception as reconciliation:
                raise RuntimeApplyFailure("Signal Processing update outcome could not be reconciled",
                                          recovery_required=True) from reconciliation
            if not isinstance(data, dict) or data.get("command_id") != exc.command_id:
                raise RuntimeApplyFailure("Signal Processing reconciliation command mismatch", recovery_required=True)
            if isinstance(data, dict) and data.get("status") == "cancelled":
                if data.get("generation") != snapshot["CONFIG.generation"] or data.get("applied_revision") != expected_revision:
                    raise RuntimeApplyFailure("Signal Processing cancellation identity mismatch", recovery_required=True)
                raise RuntimeApplyFailure("Trial boundary was not reached; update cancelled, previous pipeline retained") from exc
        self._pipeline_receipt(data, snapshot)

    @staticmethod
    def _pipeline_receipt(data, snapshot):
        if (not isinstance(data, dict) or data.get("generation") != snapshot["CONFIG.generation"]
                or data.get("applied_revision") != snapshot["CONFIG.revision"]
                or data.get("status") != "applied"):
            raise RuntimeApplyFailure("Signal Processing revision was not acknowledged", recovery_required=True)

    async def _update_source_revision(self, snapshot, expected_revision):
        data = await self._request(EventType.UpdateAcquisitionRevision, {
            "generation": snapshot["CONFIG.generation"], "expected_revision": expected_revision,
            "target_revision": snapshot["CONFIG.revision"],
        })
        if (not isinstance(data, dict) or data.get("generation") != snapshot["CONFIG.generation"]
                or data.get("config_revision") != snapshot["CONFIG.revision"]):
            raise RuntimeApplyFailure("Acquisition revision was not acknowledged", recovery_required=True)

    async def _apply_pipeline(self, stored, previous, progress):
        before = self.engine.applied_snapshot
        target = self.profiles.materialize(stored.snapshot)
        target["CONFIG.generation"] = before["CONFIG.generation"]
        progress("waiting_for_trial_boundary")
        await self._stage_pipeline(target, before["CONFIG.revision"])
        try:
            await self._update_source_revision(target, before["CONFIG.revision"])
        except Exception as exc:
            raise RuntimeApplyFailure(f"Signal Processing applied {target['CONFIG.revision']}, but acquisition acknowledgement failed: {exc}",
                                      recovery_required=True) from exc
        self.engine.acknowledge_configuration(target)
        try:
            progress("persisting")
            self.profiles.repository.save_activation(stored.snapshot, expected_revision=previous.revision)
        except Exception as exc:
            progress("rolling_back_at_trial_boundary")
            try:
                selected = self.profiles.repository.get_activation()
                if selected is None or selected.revision != previous.revision:
                    raise RuntimeError("Selected configuration was changed by another writer")
                await self._stage_pipeline(before, target["CONFIG.revision"])
                await self._update_source_revision(before, target["CONFIG.revision"])
                self.engine.acknowledge_configuration(before)
            except Exception as recovery:
                raise RuntimeApplyFailure(f"Saving failed: {exc}; pipeline recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Saving failed; previous pipeline restored: {exc}", rolled_back=True) from exc
        return {"outcome": "applied", "boundary": "trial", "session_preserved": True}

    async def _output_context(self, snapshot, expected_revision):
        data = await self._request(EventType.UpdateProcessingContext, {
            "settings_snapshot": snapshot, "expected_revision": expected_revision,
            "generation": snapshot["CONFIG.generation"],
        })
        self._pipeline_receipt(data, snapshot)

    async def _start_outputs(self, snapshot, state):
        await self.engine.restart_actuation_for_configuration(snapshot)
        if state.stimulation:
            await asyncio.to_thread(self.stimulation.handle_start_stimulation, StartStimulationCommand(),
                                     settings_snapshot=snapshot)

    async def _resume_same_source(self, state, snapshot):
        if state.checkpoint is not None:
            data = await self._request(EventType.RestoreSourceCheckpoint,
                                       {**self._identity(snapshot), "checkpoint": state.checkpoint})
            self._receipt(data, snapshot, "restored")
        await self._resume_stream(state)

    async def _apply_outputs(self, stored, previous, progress):
        before = self.engine.applied_snapshot
        target = self.profiles.materialize(stored.snapshot)
        target["CONFIG.generation"] = before["CONFIG.generation"]
        source = self.acquisition.handle_get_status(GetAcquisitionStatusQuery())
        state = _RestoreState(source.connected, source.streaming,
                              self.stimulation.is_active or self.stimulation.process_alive,
                              self.sessions.active_session_id)
        outputs_replaced = False
        processing_revision = acquisition_revision = before["CONFIG.revision"]
        resumed = persisted = False
        try:
            receipt = await self._quiesce(before, state, progress)
            progress("restarting_output_modules")
            await self._stop_stimulation()
            # Once setup of a new output driver begins, failure must be settled
            # explicitly before any old driver may be recreated.
            outputs_replaced = True
            await self._start_outputs(target, state)
            await self._output_context(target, processing_revision)
            processing_revision = target["CONFIG.revision"]
            await self._update_source_revision(target, acquisition_revision)
            acquisition_revision = target["CONFIG.revision"]
            self.engine.acknowledge_configuration(target)
            progress("persisting")
            self.profiles.repository.save_activation(stored.snapshot, expected_revision=previous.revision)
            persisted = True
            progress("restoring")
            resumed = state.streaming
            await self._resume_same_source(state, target)
            return {"outcome": "applied", "boundary": "module", "session_preserved": True,
                    "interrupted_epoch": receipt.interrupted_epoch}
        except Exception as exc:
            if (not outputs_replaced or resumed or isinstance(exc, CommandTimeoutError)
                    or getattr(exc, "recovery_required", False)):
                raise RuntimeApplyFailure(f"Output update requires recovery: {exc}", recovery_required=True) from exc
            progress("rolling_back_output_modules")
            try:
                selected = self.profiles.repository.get_activation()
                expected = stored.snapshot.revision if persisted else previous.revision
                if selected is None or selected.revision != expected:
                    raise RuntimeError("Selected configuration was changed by another writer")
                if target.get("ACTUATION.enabled", False):
                    data = await self._request(EventType.SettleActuation)
                    self._receipt(data, target, "paused", "execution_complete", "outputs_settled")
                if self.stimulation.is_active or self.stimulation.process_alive:
                    data = await self._request(EventType.PauseStimulation)
                    self._receipt(data, target, "paused", "renderer_stopped")
                await self._stop_stimulation()
                await self._start_outputs(before, state)
                if processing_revision != before["CONFIG.revision"]:
                    await self._output_context(before, processing_revision)
                if acquisition_revision != before["CONFIG.revision"]:
                    await self._update_source_revision(before, acquisition_revision)
                self.engine.acknowledge_configuration(before)
                if persisted:
                    self.profiles.repository.save_activation(previous, expected_revision=stored.snapshot.revision)
                await self._resume_same_source(state, before)
            except Exception as recovery:
                raise RuntimeApplyFailure(f"Output update failed: {exc}; recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Output update failed; previous outputs restored: {exc}", rolled_back=True) from exc

    async def _request(self, event_type, payload=None, **kwargs):
        result = await asyncio.to_thread(self.commands.request, event_type, payload, **kwargs)
        return result.data

    def _identity(self, snapshot):
        return {"generation": snapshot.get("CONFIG.generation"),
                "expected_revision": snapshot.get("CONFIG.revision")}

    @staticmethod
    def _receipt(data, snapshot, *flags):
        if (not isinstance(data, dict) or data.get("generation") != snapshot.get("CONFIG.generation")
                or any(data.get(flag) is not True for flag in flags)):
            raise RuntimeError("Worker did not acknowledge the requested safe boundary")
        return data

    async def _quiesce(self, before, state, progress):
        if state.stimulation:
            progress("pausing_stimulation")
            data = await self._request(EventType.PauseStimulation)
            self._receipt(data, before, "paused", "renderer_stopped")
        progress("draining")
        receipt = await asyncio.to_thread(self.acquisition.handle_stop_and_drain,
                                           session_id=state.session_id)
        if receipt.generation != before.get("CONFIG.generation") or receipt.config_revision != before.get("CONFIG.revision"):
            raise RuntimeError("Drain receipt belongs to a different configuration")
        if state.session_id is not None:
            trials = self.sessions.handle_get_trials(GetSessionTrialsQuery(state.session_id))
            if [trial.trial_number for trial in trials] != list(range(1, receipt.completed_trials + 1)):
                raise RuntimeError("Completed trials have not all been persisted; session remains open")
        if before.get("ACTUATION.enabled", False):
            progress("settling_actuation")
            data = await self._request(EventType.SettleActuation)
            self._receipt(data, before, "paused", "execution_complete", "outputs_settled")
        if state.connected:
            data = await self._request(EventType.GetSourceCheckpoint, self._identity(before))
            if (not isinstance(data, dict) or data.get("generation") != before.get("CONFIG.generation")
                    or data.get("config_revision") != before.get("CONFIG.revision")):
                raise RuntimeError("Source checkpoint belongs to a different configuration")
            if type(data.get("supported")) is not bool:
                raise RuntimeError("Source checkpoint support was not acknowledged")
            if data["supported"]:
                if not isinstance(data.get("checkpoint"), dict):
                    raise RuntimeError("Source did not provide a resumable checkpoint")
                state.checkpoint = data["checkpoint"]
            elif state.streaming and data.get("resume_without_checkpoint_safe") is not True:
                # A source must explicitly promise that a normal start does
                # not rewind. Otherwise retain the paused session and drivers.
                raise RuntimeApplyFailure(
                    "Automatic resume requires a checkpoint or declared live-source resume support. "
                    "The source remains paused; "
                    "stop the engine before applying this configuration.",
                    recovery_required=True,
                )
        return receipt

    async def _stop_stimulation(self):
        if self.stimulation.is_active or self.stimulation.process_alive:
            await asyncio.to_thread(self.stimulation.handle_stop_stimulation, StopStimulationCommand())

    async def _restore_ready(self, snapshot, state, previous_session_id, checkpoint,
                             *, require_live_resume=False):
        await self.engine.start_for_configuration(snapshot)
        running = self.engine.applied_snapshot
        resumed_checkpoint = False
        if state.connected:
            await asyncio.to_thread(self.acquisition.handle_connect, ConnectDataProviderCommand())
            if checkpoint is not None or require_live_resume:
                current = await self._request(EventType.GetSourceCheckpoint, self._identity(running))
                self._receipt(current, running)
                if current.get("config_revision") != running.get("CONFIG.revision"):
                    raise RuntimeError("Source resume receipt belongs to a different configuration")
                if (type(current.get("supported")) is not bool or
                        (current["supported"] and not isinstance(current.get("checkpoint"), dict))):
                    raise RuntimeError("Replacement source returned an invalid checkpoint capability")
                if require_live_resume and current.get("resume_without_checkpoint_safe") is not True:
                    raise RuntimeError("Replacement source did not confirm safe live resumption")
            if checkpoint is not None:
                self._receipt(current, running, "supported")
                selected = checkpoint_for_restore(checkpoint, current.get("checkpoint"))
                if selected is not None:
                    data = await self._request(EventType.RestoreSourceCheckpoint,
                                               {**self._identity(running), "checkpoint": selected})
                    self._receipt(data, running, "restored")
                    resumed_checkpoint = True
        if state.connected and self.signal_preparer is not None:
            await asyncio.to_thread(self.signal_preparer)
        session_id = None
        if previous_session_id is not None:
            session_id = await asyncio.to_thread(self.sessions.create_continuation, previous_session_id,
                                                 config_revision=running.get("CONFIG.revision"))
        if state.stimulation:
            await asyncio.to_thread(self.stimulation.handle_start_stimulation, StartStimulationCommand())
        return session_id, resumed_checkpoint

    async def _resume_stream(self, state):
        if state.streaming:
            await asyncio.to_thread(self.acquisition.handle_start_stream, StartStreamCommand())

    async def _rebuild(self, stored, previous, progress):
        plan, candidate = stored.plan, stored.snapshot
        before = self.engine.applied_snapshot
        target = self.profiles.materialize(candidate)
        source = self.acquisition.handle_get_status(GetAcquisitionStatusQuery())
        state = _RestoreState(source.connected, source.streaming,
                              self.stimulation.is_active or self.stimulation.process_alive,
                              self.sessions.active_session_id)
        stopped = False
        resumed = False
        persisted = False
        continuation = None
        try:
            receipt = await self._quiesce(before, state, progress)
            if state.session_id is not None:
                progress("ending_session")
                await asyncio.to_thread(self.sessions.handle_end_session, EndSessionCommand(
                    state.session_id, interruption_reason="configuration_changed",
                ))
            await self._stop_stimulation()
            progress("stopping")
            await self.engine.stop_for_configuration()
            stopped = True
            self.acquisition.reset()
            progress("starting")
            # The source validates resource identity independently of editable
            # parameters (a new chunk size must never rewind the same recording).
            old_source = next((b for b in previous.bindings if b.slot == "acquisition.source"), None)
            new_source = next((b for b in candidate.bindings if b.slot == "acquisition.source"), None)
            same_source = (old_source is not None and new_source is not None
                           and old_source.selection.package_id == new_source.selection.package_id
                           and old_source.selection.contribution_id == new_source.selection.contribution_id)
            continuation, replay_resumed = await self._restore_ready(
                target, state, state.session_id, state.checkpoint if same_source else None,
                require_live_resume=state.streaming and same_source and state.checkpoint is None,
            )
            progress("persisting")
            self.profiles.repository.save_activation(candidate, expected_revision=plan.expected_revision)
            persisted = True
            progress("restoring")
            # From this point data may produce non-replayable physical output.
            # An unknown start result must never trigger a rewind of the source.
            resumed = state.streaming
            await self._resume_stream(state)
            return {"outcome": "applied", "continuation_session_id": continuation,
                    "interrupted_epoch": receipt.interrupted_epoch,
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
                if target.get("ACTUATION.enabled", False):
                    if not self.engine.is_engine_ready:
                        raise RuntimeError("Candidate actuator startup outcome is unknown; cannot recreate an output driver")
                    running = self.engine.applied_snapshot
                    data = await self._request(EventType.SettleActuation)
                    self._receipt(data, running, "paused", "execution_complete", "outputs_settled")
                if self.stimulation.is_active or self.stimulation.process_alive:
                    data = await self._request(EventType.PauseStimulation)
                    self._receipt(data, self.engine.applied_snapshot, "paused", "renderer_stopped")
                await self._stop_stimulation()
                active = self.sessions.active_session_id
                previous_session_id = state.session_id
                if active is not None and self.engine.is_engine_ready:
                    await asyncio.to_thread(self.sessions.handle_end_session, EndSessionCommand(
                        active, interruption_reason="configuration_rollback",
                    ))
                    previous_session_id = active
                await self.engine.stop_for_configuration()
                self.acquisition.reset()
                if persisted:
                    self.profiles.repository.save_activation(previous, expected_revision=candidate.revision)
                accepted = self.profiles.repository.get_activation()
                if accepted is None or accepted.revision != previous.revision:
                    raise RuntimeError("Selected configuration was changed by another writer")
                await self._restore_ready(
                    before, state, previous_session_id, state.checkpoint,
                    require_live_resume=state.streaming and state.checkpoint is None,
                )
                await self._resume_stream(state)
            except BaseException as recovery:
                raise RuntimeApplyFailure(f"Apply failed: {exc}; recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Apply failed; previous configuration restored: {exc}",
                                      rolled_back=True) from exc
