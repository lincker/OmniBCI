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
    supported_strategies = {"select_on_start", "no_change", "rebuild"}

    def __init__(self, engine, profiles, acquisition, sessions, stimulation, commands):
        self.engine = engine
        self.profiles = profiles
        self.acquisition = acquisition
        self.sessions = sessions
        self.stimulation = stimulation
        self.commands = commands

    async def execute(self, stored, progress):
        plan, candidate = stored.plan, stored.snapshot
        async with self.engine.runtime_transition():
            previous = self.profiles.repository.get_activation()
            if ((previous.revision if previous else None) != plan.expected_revision
                    or self.engine.is_engine_ready != plan.running
                    or (plan.running and (self.engine.generation != plan.generation
                                          or self.engine.applied_revision != plan.applied_revision))):
                raise ProfileRevisionConflict("Runtime changed before configuration application")
            self.profiles.validate(candidate, require_available=True)
            if plan.strategy in ("select_on_start", "no_change"):
                progress("persisting")
                self.profiles.repository.save_activation(candidate, expected_revision=plan.expected_revision)
                return {"outcome": "selected" if not plan.running else "applied"}
            if plan.strategy not in self.supported_strategies:
                raise ValueError("The planned update boundary is unavailable")
            return await self._rebuild(stored, previous, progress)

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
            state.checkpoint = data.get("checkpoint") if data.get("supported") else None
        return receipt

    async def _stop_stimulation(self):
        if self.stimulation.is_active or self.stimulation.process_alive:
            await asyncio.to_thread(self.stimulation.handle_stop_stimulation, StopStimulationCommand())

    async def _restore_ready(self, snapshot, state, previous_session_id, checkpoint):
        await self.engine.start_for_configuration(snapshot)
        running = self.engine.applied_snapshot
        resumed_checkpoint = False
        if state.connected:
            await asyncio.to_thread(self.acquisition.handle_connect, ConnectDataProviderCommand())
            if checkpoint is not None:
                current = await self._request(EventType.GetSourceCheckpoint, self._identity(running))
                self._receipt(current, running, "supported")
                selected = checkpoint_for_restore(checkpoint, current.get("checkpoint"))
                if selected is not None:
                    data = await self._request(EventType.RestoreSourceCheckpoint,
                                               {**self._identity(running), "checkpoint": selected})
                    self._receipt(data, running, "restored")
                    resumed_checkpoint = True
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
                await self._restore_ready(before, state, previous_session_id, state.checkpoint)
                await self._resume_stream(state)
            except BaseException as recovery:
                raise RuntimeApplyFailure(f"Apply failed: {exc}; recovery failed: {recovery}",
                                          recovery_required=True) from recovery
            raise RuntimeApplyFailure(f"Apply failed; previous configuration restored: {exc}",
                                      rolled_back=True) from exc
